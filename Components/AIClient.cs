using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace ShadowPresident;

internal static class AIClient
{
    private static readonly HttpClient _http   = CreateClient();
    private static readonly Queue<string> _context    = new();
    private static readonly Queue<string> _codexIds   = new();
    private const int MaxRecentCodexIds = 10;

    // Number of times to retry a /decision request before giving up and pausing.
    private const int MaxDecisionAttempts = 4;

    private static HttpClient CreateClient()
    {
        var c = new HttpClient { Timeout = TimeSpan.FromSeconds(60) };
        // Disable keep-alive so every request uses a fresh connection. Reusing a
        // pooled socket after a prior request — especially two decisions firing in
        // rapid succession — can surface "the I/O operation has been aborted"
        // (ERROR_OPERATION_ABORTED) when the previous keep-alive socket is stale.
        c.DefaultRequestHeaders.ConnectionClose = true;
        return c;
    }

    internal static string ModelName { get; private set; } = "AI";
    internal static int LastPromptTokens { get; private set; }
    internal static int LastCompletionTokens { get; private set; }
    internal static int CurrentTurn { get; set; }
    internal static int CurrentStep { get; set; }
    internal static string CurrentFragment { get; set; } = "";

    internal static void AddContext(string speaker, string text)
    {
        _context.Enqueue($"{speaker}: {text}");
        while (_context.Count > Plugin.AIContextLines.Value)
            _context.Dequeue();
    }

    internal static string CurrentPhase   { get; set; } = "main";
    internal static string CurrentStats   { get; set; } = "";
    internal static string CurrentNews    { get; set; } = "";
    internal static string CurrentReports { get; set; } = "";

    internal static void AddCodexRef(string articyId)
    {
        if (string.IsNullOrWhiteSpace(articyId)) { return; }
        // Deduplicate: remove existing occurrence so we re-add at the back (most recent).
        var tmp = new List<string>(_codexIds);
        tmp.Remove(articyId);
        _codexIds.Clear();
        foreach (var id in tmp) { _codexIds.Enqueue(id); }
        _codexIds.Enqueue(articyId);
        while (_codexIds.Count > MaxRecentCodexIds) { _codexIds.Dequeue(); }
    }

    internal static (int index, string reasoning)? RequestDecision(
        string type, List<(int index, string text)> choices)
    {
        var url = Plugin.AIServerUrl.Value.TrimEnd('/') + "/decision";

        var contextJson = BuildJsonArray(_context);
        var choicesJson = BuildChoicesJson(choices);
        var statsJson   = string.IsNullOrEmpty(CurrentStats)   || CurrentPhase == "prologue" ? "\"\"" : $"\"{EscapeJson(CurrentStats)}\"";
        var newsJson    = string.IsNullOrEmpty(CurrentNews)    || CurrentPhase == "prologue" ? "\"\"" : $"\"{EscapeJson(CurrentNews)}\"";
        var reportsJson = string.IsNullOrEmpty(CurrentReports) || CurrentPhase == "prologue" ? "\"\"" : $"\"{EscapeJson(CurrentReports)}\"";
        var codexJson   = BuildJsonArray(_codexIds);
        var body = $"{{\"type\":\"{EscapeJson(type)}\"," +
                   $"\"phase\":\"{CurrentPhase}\"," +
                   $"\"turn\":{CurrentTurn},\"step\":{CurrentStep}," +
                   $"\"fragment\":\"{EscapeJson(CurrentFragment)}\"," +
                   $"\"stats\":{statsJson}," +
                   $"\"news\":{newsJson}," +
                   $"\"reports\":{reportsJson}," +
                   $"\"codex_refs\":{codexJson}," +
                   $"\"context\":{contextJson},\"choices\":{choicesJson}}}";

        for (int attempt = 1; attempt <= MaxDecisionAttempts; attempt++)
        {
            try
            {
                var response = _http
                    .PostAsync(url, new StringContent(body, Encoding.UTF8, "application/json"))
                    .GetAwaiter().GetResult();

                var responseBody = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();

                if (!response.IsSuccessStatusCode)
                {
                    Plugin.Log.LogWarning($"[AI] Server returned {(int)response.StatusCode} " +
                        $"(attempt {attempt}/{MaxDecisionAttempts}): {responseBody}");
                    if (RetryAfter(attempt)) { continue; }
                    return null;
                }

                var indexMatch = Regex.Match(responseBody, @"""choice_index""\s*:\s*(\d+)");
                if (!indexMatch.Success)
                {
                    // Server responded but the body is malformed — retrying may yield a
                    // valid completion (e.g. the model returned non-JSON).
                    Plugin.Log.LogWarning($"[AI] Could not parse choice_index " +
                        $"(attempt {attempt}/{MaxDecisionAttempts}) from: {responseBody}");
                    if (RetryAfter(attempt)) { continue; }
                    return null;
                }

                int index = Math.Max(0, Math.Min(int.Parse(indexMatch.Groups[1].Value), choices.Count - 1));

                var reasoningMatch = Regex.Match(responseBody, @"""reasoning""\s*:\s*""((?:[^""\\]|\\.)*)""");
                string reasoning = reasoningMatch.Success
                    ? reasoningMatch.Groups[1].Value.Replace("\\\"", "\"").Replace("\\n", " ").Trim()
                    : "";

                var modelMatch = Regex.Match(responseBody, @"""model_name""\s*:\s*""((?:[^""\\]|\\.)*)""");
                if (modelMatch.Success && !string.IsNullOrWhiteSpace(modelMatch.Groups[1].Value))
                    ModelName = modelMatch.Groups[1].Value.Trim();

                var promptMatch = Regex.Match(responseBody, @"""prompt_tokens""\s*:\s*(\d+)");
                var complMatch = Regex.Match(responseBody, @"""completion_tokens""\s*:\s*(\d+)");
                if (promptMatch.Success) LastPromptTokens = int.Parse(promptMatch.Groups[1].Value);
                if (complMatch.Success) LastCompletionTokens = int.Parse(complMatch.Groups[1].Value);

                if (!string.IsNullOrWhiteSpace(reasoning))
                    Plugin.Log.LogMessage($"[{ModelName}] {reasoning}");

                return (index, reasoning);
            }
            catch (Exception ex)
            {
                Plugin.Log.LogWarning($"[AI] Cannot reach server at {url} " +
                    $"(attempt {attempt}/{MaxDecisionAttempts}): {ex.GetBaseException().Message}");
                if (RetryAfter(attempt)) { continue; }
                return null;
            }
        }

        return null;
    }

    // Sleeps with a short backoff between retries. Runs on the background Task thread
    // (drivers call RequestDecision via Task.Run), so this never blocks the game thread.
    // Returns false when no attempts remain.
    private static bool RetryAfter(int attempt)
    {
        if (attempt >= MaxDecisionAttempts) { return false; }
        Thread.Sleep(500 * attempt); // 500ms, 1s, 1.5s …
        return true;
    }

    internal static void PostCheckpoint()
    {
        var url = Plugin.AIServerUrl.Value.TrimEnd('/') + "/checkpoint";
        var contextJson = BuildJsonArray(_context);
        var body = $"{{\"turn\":{CurrentTurn},\"step\":{CurrentStep}," +
                   $"\"fragment\":\"{EscapeJson(CurrentFragment)}\"," +
                   $"\"context\":{contextJson}}}";
        try
        {
            _http.PostAsync(url, new StringContent(body, Encoding.UTF8, "application/json"))
                 .GetAwaiter().GetResult();
        }
        catch { /* fire-and-forget — don't block or crash on checkpoint failure */ }
    }

    internal static void PostStats(string stats, string news = "", string reports = "")
    {
        var url = Plugin.AIServerUrl.Value.TrimEnd('/') + "/stats";
        var body = $"{{\"stats\":\"{EscapeJson(stats)}\",\"news\":\"{EscapeJson(news)}\",\"reports\":\"{EscapeJson(reports)}\"}}";
        try
        {
            _http.PostAsync(url, new StringContent(body, Encoding.UTF8, "application/json"))
                 .GetAwaiter().GetResult();
        }
        catch { /* fire-and-forget */ }
    }

    internal static void LogHumanChoice(string type, List<(int index, string text)> choices, int chosenIndex)
    {
        var url = Plugin.AIServerUrl.Value.TrimEnd('/') + "/decision";
        var contextJson = BuildJsonArray(_context);
        var choicesJson = BuildChoicesJson(choices);
        var body = $"{{\"type\":\"{EscapeJson(type)}\"," +
                          $"\"phase\":\"{CurrentPhase}\"," +
                          $"\"turn\":{CurrentTurn},\"step\":{CurrentStep}," +
                          $"\"fragment\":\"{EscapeJson(CurrentFragment)}\"," +
                          $"\"context\":{contextJson},\"choices\":{choicesJson}," +
                          $"\"choice_index\":{chosenIndex}}}";
        try
        {
            _http.PostAsync(url, new StringContent(body, Encoding.UTF8, "application/json"))
                 .GetAwaiter().GetResult();
        }
        catch { /* fire-and-forget */ }
    }

    private static string BuildJsonArray(IEnumerable<string> items)
    {
        var sb = new StringBuilder("[");
        bool first = true;
        foreach (var item in items)
        {
            if (!first) { sb.Append(','); }
            sb.Append('"').Append(EscapeJson(item)).Append('"');
            first = false;
        }
        sb.Append(']');
        return sb.ToString();
    }

    private static string BuildChoicesJson(List<(int index, string text)> choices)
    {
        var sb = new StringBuilder("[");
        for (int i = 0; i < choices.Count; i++)
        {
            if (i > 0) { sb.Append(','); }
            sb.Append($"{{\"index\":{choices[i].index},\"text\":\"{EscapeJson(choices[i].text)}\"}}");
        }
        sb.Append(']');
        return sb.ToString();
    }

    private static string EscapeJson(string s) =>
        s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", "\\n").Replace("\r", "");
}
