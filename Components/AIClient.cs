using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace ShadowPresident;

internal static class AIClient
{
    // Two separate clients so the long, response-critical /decision (and /resume) request never
    // shares connection state with the fire-and-forget /stats and /checkpoint posts. Those posts
    // fire on their own background Tasks (every 30s for stats, per-fragment for checkpoints) and
    // routinely overlap a slow LLM decision; sharing one client made them contend on the same
    // pooled/closing socket, which surfaced "the I/O operation has been aborted"
    // (ERROR_OPERATION_ABORTED) on the decision request.
    private static readonly HttpClient _decisionHttp = CreateDecisionClient();
    private static readonly HttpClient _bgHttp       = CreateBackgroundClient();
    private static readonly Queue<string> _context    = new();
    private static readonly Queue<string> _codexIds   = new();
    private const int MaxRecentCodexIds = 14;

    // Number of times to retry a /decision request before giving up and pausing.
    private const int MaxDecisionAttempts = 4;

    private static HttpClient CreateDecisionClient()
    {
        var c = new HttpClient { Timeout = TimeSpan.FromSeconds(120) };
        // Fresh connection per decision (no keep-alive). Werkzeug's threaded dev server handles
        // idle keep-alive connections unreliably, so reusing one can surface ERROR_OPERATION_ABORTED
        // on the next decision. A socket abort is now harmless anyway: the server coalesces a retry
        // onto the still-running original (request_id), so we never trigger a duplicate LLM call —
        // the retry just waits and gets the same answer.
        c.DefaultRequestHeaders.ConnectionClose = true;
        return c;
    }

    private static HttpClient CreateBackgroundClient()
    {
        var c = new HttpClient { Timeout = TimeSpan.FromSeconds(120) };
        // Fire-and-forget posts are sparse (≥30s apart), so a kept-alive socket goes stale between
        // them — reusing it surfaced ERROR_OPERATION_ABORTED. Close the connection each time; these
        // calls don't need a response and any failure is swallowed, so there's no retry to dirty.
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

    // Seed the rolling context from the server's restored window after a restart. The server
    // reconstructs the dialogue leading up to the last checkpoint (the autosave the game
    // replays), so the first post-reload decision isn't context-starved. No-op for a fresh
    // run (the server returns an empty list). Runs once on a background Task from Plugin.Awake.
    internal static void FetchResume()
    {
        if (!Plugin.UseAIServer.Value) { return; }
        var url = Plugin.AIServerUrl.Value.TrimEnd('/') + "/resume";
        try
        {
            var response = _decisionHttp.GetAsync(url).GetAwaiter().GetResult();
            if (!response.IsSuccessStatusCode) { return; }
            var body = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();

            var arrMatch = Regex.Match(body, @"""context""\s*:\s*\[(.*)\]", RegexOptions.Singleline);
            if (!arrMatch.Success) { return; }

            int seeded = 0;
            foreach (Match m in Regex.Matches(arrMatch.Groups[1].Value, @"""((?:[^""\\]|\\.)*)"""))
            {
                var line = JsonUnescape(m.Groups[1].Value);
                if (string.IsNullOrEmpty(line)) { continue; }
                _context.Enqueue(line);
                seeded++;
            }
            while (_context.Count > Plugin.AIContextLines.Value) { _context.Dequeue(); }
            if (seeded > 0) { Plugin.Log.LogInfo($"[AI] Seeded {seeded} context lines from /resume."); }
        }
        catch (Exception ex)
        {
            Plugin.Log.LogWarning($"[AI] /resume failed: {ex.GetBaseException().Message}");
        }
    }

    internal static string CurrentPhase   { get; set; } = "main";
    internal static string CurrentStats   { get; set; } = "";
    internal static string CurrentNews    { get; set; } = "";
    internal static string CurrentReports { get; set; } = "";

    // Journal ledger (terse turn-stamped facts) and economy/approval graph trajectories.
    // Populated by JournalReader / GraphReader; shipped with /decision. The server injects the
    // journal as a system-prompt ledger block and the economy at the tail of the user block.
    internal static string CurrentJournal { get; set; } = "";
    internal static string CurrentEconomy { get; set; } = "";

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

    // `question` is the prompt the panel is actually asking right now (empty for decision types
    // whose question is already the tail of the dialogue). It is sent as its own field rather than
    // pushed onto the rolling context: the server renders it in the instruction slot above the
    // choices, so the context stays a log of *settled* exchanges. See PagedDecisionDriver.
    internal static (int index, string reasoning)? RequestDecision(
        string type, List<(int index, string text)> choices, string question = "")
    {
        var raw = PostDecision(type, choices, "", question,
            body => Regex.IsMatch(body, "\"choice_index\"\\s*:\\s*\\d+"));
        if (string.IsNullOrEmpty(raw)) { return null; }

        var indexMatch = Regex.Match(raw, @"""choice_index""\s*:\s*(\d+)");
        int index = indexMatch.Success
            ? Math.Max(0, Math.Min(int.Parse(indexMatch.Groups[1].Value), choices.Count - 1))
            : 0;
        string reasoning = ParseReasoning(raw);
        if (!string.IsNullOrWhiteSpace(reasoning)) { Plugin.Log.LogMessage($"[{ModelName}] {reasoning}"); }
        return (index, reasoning);
    }

    // Multi-select variant for checkbox pages (e.g. emergency decrees). The model returns a set of
    // indices ("choice_indices"); minSelect/maxSelect are the page's Minimum/MaximumChoiceCount and
    // the server clamps the set into that range (padding up to the minimum) before replying.
    internal static (List<int> indices, string reasoning)? RequestMultiDecision(
        string type, List<(int index, string text)> choices, int minSelect, int maxSelect,
        string question = "")
    {
        var extra = $"\"min_select\":{minSelect},\"max_select\":{maxSelect},";
        var raw = PostDecision(type, choices, extra, question,
            body => Regex.IsMatch(body, "\"choice_indices\"\\s*:\\s*\\["));
        if (string.IsNullOrEmpty(raw)) { return null; }

        var indices = new List<int>();
        var arrMatch = Regex.Match(raw, @"""choice_indices""\s*:\s*\[([^\]]*)\]");
        if (arrMatch.Success)
        {
            foreach (Match n in Regex.Matches(arrMatch.Groups[1].Value, @"\d+"))
            {
                int v = int.Parse(n.Value);
                if (v >= 0 && v < choices.Count && !indices.Contains(v)) { indices.Add(v); }
            }
        }
        // The server is authoritative on min/max, but never let a bad reply over-check the page.
        if (maxSelect > 0 && indices.Count > maxSelect) { indices = indices.GetRange(0, maxSelect); }

        string reasoning = ParseReasoning(raw);
        if (!string.IsNullOrWhiteSpace(reasoning)) { Plugin.Log.LogMessage($"[{ModelName}] {reasoning}"); }
        return (indices, reasoning);
    }

    // Shared /decision POST: builds the request body, runs the retry loop, parses the common meta
    // (model name + token counts) and returns the raw response body on success, or "" after
    // exhausting retries. `extraFields` is JSON injected before context/choices (must end with a
    // trailing comma, or be empty). `isComplete` decides whether a 2xx body actually carries a
    // usable answer — if not, it's treated like a malformed completion and retried.
    private static string PostDecision(
        string type, List<(int index, string text)> choices, string extraFields, string question,
        Func<string, bool> isComplete)
    {
        var url = Plugin.AIServerUrl.Value.TrimEnd('/') + "/decision";

        var contextJson = BuildJsonArray(_context);
        var choicesJson = BuildChoicesJson(choices);
        var statsJson   = string.IsNullOrEmpty(CurrentStats)   || CurrentPhase == "prologue" ? "\"\"" : $"\"{EscapeJson(CurrentStats)}\"";
        var newsJson    = string.IsNullOrEmpty(CurrentNews)    || CurrentPhase == "prologue" ? "\"\"" : $"\"{EscapeJson(CurrentNews)}\"";
        var reportsJson = string.IsNullOrEmpty(CurrentReports) || CurrentPhase == "prologue" ? "\"\"" : $"\"{EscapeJson(CurrentReports)}\"";
        var journalJson = string.IsNullOrEmpty(CurrentJournal) || CurrentPhase == "prologue" ? "\"\"" : $"\"{EscapeJson(CurrentJournal)}\"";
        var economyJson = string.IsNullOrEmpty(CurrentEconomy) || CurrentPhase == "prologue" ? "\"\"" : $"\"{EscapeJson(CurrentEconomy)}\"";
        var codexJson   = BuildJsonArray(_codexIds);
        // One stable id per decision, reused across every retry attempt below. The server caches its
        // response under this id and replays it on a duplicate (idempotent /decision), so a socket
        // abort that fires after the server already logged the decision can't produce a second,
        // duplicate decision row when we retry.
        var requestId   = Guid.NewGuid().ToString("N");
        var body = $"{{\"type\":\"{EscapeJson(type)}\"," +
                   $"\"request_id\":\"{requestId}\"," +
                   $"\"phase\":\"{CurrentPhase}\"," +
                   $"\"turn\":{CurrentTurn},\"step\":{CurrentStep}," +
                   $"\"fragment\":\"{EscapeJson(CurrentFragment)}\"," +
                   $"\"question\":\"{EscapeJson(question)}\"," +
                   $"\"stats\":{statsJson}," +
                   $"\"news\":{newsJson}," +
                   $"\"reports\":{reportsJson}," +
                   $"\"journal\":{journalJson}," +
                   $"\"economy\":{economyJson}," +
                   $"\"codex_refs\":{codexJson}," +
                   extraFields +
                   $"\"context\":{contextJson},\"choices\":{choicesJson}}}";

        for (int attempt = 1; attempt <= MaxDecisionAttempts; attempt++)
        {
            try
            {
                var response = _decisionHttp
                    .PostAsync(url, new StringContent(body, Encoding.UTF8, "application/json"))
                    .GetAwaiter().GetResult();

                var responseBody = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();

                if (!response.IsSuccessStatusCode)
                {
                    Plugin.Log.LogWarning($"[AI] Server returned {(int)response.StatusCode} " +
                        $"(attempt {attempt}/{MaxDecisionAttempts}): {responseBody}");
                    if (RetryAfter(attempt)) { continue; }
                    return "";
                }

                if (!isComplete(responseBody))
                {
                    // Server responded but the body lacks a usable answer (e.g. the model returned
                    // non-JSON) — retrying may yield a valid completion.
                    Plugin.Log.LogWarning($"[AI] Could not parse decision " +
                        $"(attempt {attempt}/{MaxDecisionAttempts}) from: {responseBody}");
                    if (RetryAfter(attempt)) { continue; }
                    return "";
                }

                ParseDecisionMeta(responseBody);
                return responseBody;
            }
            catch (Exception ex)
            {
                Plugin.Log.LogWarning($"[AI] Cannot reach server at {url} " +
                    $"(attempt {attempt}/{MaxDecisionAttempts}): {ex.GetBaseException().Message}");
                if (RetryAfter(attempt)) { continue; }
                return "";
            }
        }

        return "";
    }

    private static void ParseDecisionMeta(string responseBody)
    {
        var modelMatch = Regex.Match(responseBody, @"""model_name""\s*:\s*""((?:[^""\\]|\\.)*)""");
        if (modelMatch.Success && !string.IsNullOrWhiteSpace(modelMatch.Groups[1].Value))
            ModelName = modelMatch.Groups[1].Value.Trim();

        var promptMatch = Regex.Match(responseBody, @"""prompt_tokens""\s*:\s*(\d+)");
        var complMatch  = Regex.Match(responseBody, @"""completion_tokens""\s*:\s*(\d+)");
        if (promptMatch.Success) LastPromptTokens = int.Parse(promptMatch.Groups[1].Value);
        if (complMatch.Success)  LastCompletionTokens = int.Parse(complMatch.Groups[1].Value);
    }

    private static string ParseReasoning(string responseBody)
    {
        var reasoningMatch = Regex.Match(responseBody, @"""reasoning""\s*:\s*""((?:[^""\\]|\\.)*)""");
        return reasoningMatch.Success
            ? JsonUnescape(reasoningMatch.Groups[1].Value).Replace('\n', ' ').Replace('\r', ' ').Trim()
            : "";
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
            _bgHttp.PostAsync(url, new StringContent(body, Encoding.UTF8, "application/json"))
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
            _bgHttp.PostAsync(url, new StringContent(body, Encoding.UTF8, "application/json"))
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
            _bgHttp.PostAsync(url, new StringContent(body, Encoding.UTF8, "application/json"))
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

    // Decode the contents of a JSON string (the part between the quotes) into a .NET string.
    // We hand-parse server responses with regex rather than a JSON library, so we must undo JSON
    // string escaping ourselves — including \uXXXX, which Flask's jsonify emits for every non-ASCII
    // char (so the curly quotes “ ” arrive as “ / ”). Without decoding \u, restored
    // context and reasoning show the literal escape text instead of the character.
    private static string JsonUnescape(string s)
    {
        if (string.IsNullOrEmpty(s) || s.IndexOf('\\') < 0) { return s; }
        var sb = new StringBuilder(s.Length);
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            if (c != '\\' || i + 1 >= s.Length) { sb.Append(c); continue; }
            char n = s[++i];
            switch (n)
            {
                case '"':  sb.Append('"');  break;
                case '\\': sb.Append('\\'); break;
                case '/':  sb.Append('/');  break;
                case 'n':  sb.Append('\n'); break;
                case 'r':  sb.Append('\r'); break;
                case 't':  sb.Append('\t'); break;
                case 'b':  sb.Append('\b'); break;
                case 'f':  sb.Append('\f'); break;
                case 'u':
                    if (i + 4 < s.Length && int.TryParse(
                            s.Substring(i + 1, 4),
                            System.Globalization.NumberStyles.HexNumber,
                            System.Globalization.CultureInfo.InvariantCulture,
                            out int code))
                    {
                        sb.Append((char)code);
                        i += 4;
                    }
                    else { sb.Append(n); }
                    break;
                default: sb.Append(n); break;
            }
        }
        return sb.ToString();
    }
}
