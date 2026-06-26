using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
using UnityEngine;

namespace ShadowPresident;

public class CodexReader : MonoBehaviour
{
    public CodexReader(IntPtr ptr) : base(ptr) { }

    private static readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(30) };

    // IDs seen in dialogue but not yet looked up in game memory.
    internal static readonly Queue<string>   _pending = new();
    // IDs already dispatched to server — never re-queued.
    private  static readonly HashSet<string> _sent    = new();

    private float _nextAt = 0f;
    private CodexPanel _panel;

    void Awake()
    {
        System.Threading.Tasks.Task.Run(PreloadKnownIds);
    }

    private static void PreloadKnownIds()
    {
        try
        {
            var url      = Plugin.AIServerUrl.Value.TrimEnd('/') + "/codex/ids";
            var response = _http.GetAsync(url).GetAwaiter().GetResult();
            var body     = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            var match    = System.Text.RegularExpressions.Regex.Matches(body, @"""([^""]+)""");
            int count    = 0;
            lock (_sent)
            {
                foreach (System.Text.RegularExpressions.Match m in match)
                {
                    if (_sent.Add(m.Groups[1].Value)) { count++; }
                }
            }
            Plugin.Log.LogInfo($"[CodexReader] Pre-seeded {count} known IDs from server cache.");
        }
        catch (System.Exception ex)
        {
            Plugin.Log.LogWarning($"[CodexReader] Could not pre-load codex IDs: {ex.GetBaseException().Message}");
        }
    }

    void Update()
    {
        if (Time.time < _nextAt) { return; }
        if (_pending.Count == 0) { return; }

        if (_panel == null) { _panel = FindObjectOfType<CodexPanel>(); }
        if (_panel == null) { return; }

        // GoToCodexEntryByArticyId → GoToCodexEntry → CodexEntrySetupPatch.Prefix
        // intercepts the data and cancels the UI call, so nothing shows on screen.
        string id = _pending.Dequeue();
        _panel.GoToCodexEntryByArticyId(id);
        _nextAt = Time.time + 0.1f; // small gap to avoid hammering in one frame
    }

    /// Called from ConversationLinePatch when a <link="id"> is found in dialogue.
    internal static void QueueLookup(string id)
    {
        if (string.IsNullOrWhiteSpace(id)) { return; }
        lock (_sent)
        {
            if (_sent.Contains(id)) { return; }
            _pending.Enqueue(id);
        }
        Plugin.Log.LogInfo($"[CodexReader] Queued: {id}");
    }

    /// Called from CodexEntrySetupPatch when an entry is displayed.
    internal static void PostEntry(string articyId, string nameInDb, string title, string desc)
    {
        lock (_sent) { _sent.Add(articyId); }

        var url  = Plugin.AIServerUrl.Value.TrimEnd('/') + "/codex";
        var body = $"{{\"articy_id\":\"{Esc(articyId)}\"," +
                   $"\"name_in_db\":\"{Esc(nameInDb)}\"," +
                   $"\"title\":\"{Esc(title)}\"," +
                   $"\"raw\":\"{Esc(desc)}\"}}";
        try
        {
            _http.PostAsync(url, new StringContent(body, Encoding.UTF8, "application/json"))
                 .GetAwaiter().GetResult();
        }
        catch { /* fire-and-forget */ }
    }

    private static string Esc(string s) =>
        s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", "\\n").Replace("\r", "");
}
