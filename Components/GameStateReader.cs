using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using UnityEngine;

namespace ShadowPresident;

public class GameStateReader : MonoBehaviour
{
    public GameStateReader(IntPtr ptr) : base(ptr) { }

    private float _nextReadTime = 0f;
    private const float ReadInterval = 30f;

    // Set by ScheduleEarlyRead() to force a ReadStats() sooner than the 30s interval.
    private static float _earlyReadAt = 0f;

    // Called from CheckpointPatch after dismissing old reports so ReadStats() fires
    // soon enough to pick up any new reports the game adds right after the checkpoint.
    public static void ScheduleEarlyRead(float delaySeconds = 1.5f)
    {
        _earlyReadAt = Time.time + delaySeconds;
    }

    void Update()
    {
        float now = Time.time;
        bool forced = _earlyReadAt > 0f && now >= _earlyReadAt;
        if (forced) { _earlyReadAt = 0f; _nextReadTime = now + ReadInterval; }
        else if (now < _nextReadTime) { return; }
        else { _nextReadTime = now + ReadInterval; }
        ReadStats();
    }

    private unsafe void ReadStats()
    {
        // Read the LIVE HUD stats straight from HUDPanel's instantiated lists. The old
        // FindObjectsOfType<TemplateHUDStat>(true) approach also swept in the templateHUDStat
        // prefab (0x88) — which holds a stale serialized value — plus the inactive war-resource
        // stats and any tooltip/detail-panel copies. The shared `seen` dedup then kept whichever
        // duplicate sorted first, so the stale template could permanently shadow the live stat
        // (e.g. Government Budget frozen at -9). The instantiated lists are the actual on-screen
        // stats and are re-Setup() as values change.
        var hud = FindHUDPanel();
        if (hud == null) { return; }

        var parts = new List<string>();
        var seen = new HashSet<string>(System.StringComparer.OrdinalIgnoreCase);

        int numCount = 0, textCount = 0;

        nint numListPtr = *(nint*)(hud.Pointer + 0x90);   // instantiatedHUDStats
        if (numListPtr != 0)
        {
            var numStats = new Il2CppSystem.Collections.Generic.List<TemplateHUDStat>(numListPtr);
            numCount = numStats.Count;
            for (int i = 0; i < numStats.Count; i++)
            {
                var stat = numStats[i];
                if (stat == null) { continue; }

                string name = ReadStatName(stat);
                string display = ConversationLinePatch.StripTags(ReadTMP(stat.Pointer + 0x20));
                Plugin.Log.LogDebug($"[GameStateReader] numStat name='{name}' val='{display}'");
                if (string.IsNullOrWhiteSpace(name) || string.IsNullOrWhiteSpace(display)) { continue; }
                if (!seen.Add(name)) { continue; }
                parts.Add($"{name}: {display}");
            }
        }

        nint textListPtr = *(nint*)(hud.Pointer + 0x80);  // instantiatedHUDTextStats
        if (textListPtr != 0)
        {
            var textStats = new Il2CppSystem.Collections.Generic.List<TemplateHUDTextStat>(textListPtr);
            textCount = textStats.Count;
            for (int i = 0; i < textStats.Count; i++)
            {
                var stat = textStats[i];
                if (stat == null) { continue; }

                // Try data chain first; fall back to the statNameText TMP at 0x20.
                string name = ReadTextStatName(stat);
                if (string.IsNullOrWhiteSpace(name)) { name = ReadTMP(stat.Pointer + 0x20); }

                string val = ConversationLinePatch.StripTags(ReadTMP(stat.Pointer + 0x28));
                Plugin.Log.LogDebug($"[GameStateReader] textStat name='{name}' val='{val}'");
                if (string.IsNullOrWhiteSpace(name) || string.IsNullOrWhiteSpace(val)) { continue; }
                if (!seen.Add(name)) { continue; }
                parts.Add($"{name}: {val}");
            }
        }

        Plugin.Log.LogInfo($"[GameStateReader] ReadStats: {numCount} num, {textCount} text → {parts.Count} parts");

        if (parts.Count > 0)
        {
            string result = string.Join(" | ", parts);
            bool statsChanged = result != AIClient.CurrentStats;
            if (statsChanged)
            {
                AIClient.CurrentStats = result;
                Plugin.Log.LogInfo($"[GameStateReader] Stats: {result}");
            }

            string news = ReadArticles(AIClient.CurrentTurn);
            bool newsChanged = news != AIClient.CurrentNews;
            if (newsChanged) { AIClient.CurrentNews = news; }

            var gfm = FindObjectOfType<GameFlowManager>();
            string reports = ReadReports(gfm, AIClient.CurrentTurn);
            bool reportsChanged = reports != AIClient.CurrentReports;
            if (reportsChanged) { AIClient.CurrentReports = reports; }

            if (statsChanged || newsChanged || reportsChanged)
            {
                string s = AIClient.CurrentStats;
                string n = AIClient.CurrentNews;
                string r = AIClient.CurrentReports;
                Task.Run(() => AIClient.PostStats(s, n, r));
            }
        }
    }

    // Returns the active/showing HUDPanel (the live national-map HUD). FindObjectsOfType(true)
    // can return more than one (e.g. an inactive cached panel); prefer the one that IsShowing().
    private static HUDPanel FindHUDPanel()
    {
        var panels = FindObjectsOfType<HUDPanel>(true);
        if (panels == null || panels.Length == 0) { return null; }

        HUDPanel fallback = null;
        foreach (var p in panels)
        {
            if (p == null) { continue; }
            if (fallback == null) { fallback = p; }
            if (p.IsShowing()) { return p; }
        }
        return fallback;
    }

    // ── Newspaper reading ─────────────────────────────────────────────────────

    private const int ArticleMaxChars  = 300;
    private const int ArticlesPerRead  = 2;

    // Rotating-window cursor for newspaper articles. _newsOffset marches forward through
    // the current turn's article list one read at a time so the AI sees fresh papers as a
    // turn progresses (a turn spans many events and the papers keep updating). _newsTurn
    // detects a turn change so the cursor resets when a new turn's papers replace the set.
    private static int _newsTurn   = -1;
    private static int _newsOffset = 0;

    private static unsafe string ReadArticles(int currentTurn)
    {
        if (currentTurn <= 0) { return ""; }

        var news = EntityDataManager.NewsData;
        if (news == null) { return ""; }

        var articles = new List<(int idx, string paper, string title, string desc)>();

        for (int i = 0; i < news.Count; i++)
        {
            var article = news[i];
            if (article == null || !article.IsEnabled) { continue; }

            nint propsPtr = *(nint*)(article.Pointer + 0x38);  // NewsProperties
            if (propsPtr == 0) { continue; }
            if (*(int*)(propsPtr + 0x10) != currentTurn) { continue; }  // TurnNo

            string title = ReadIl2CppString(propsPtr + 0x20);  // Title
            if (string.IsNullOrWhiteSpace(title)) { continue; }

            string desc  = TruncateAtSentence(ReadIl2CppString(propsPtr + 0x28), ArticleMaxChars);  // Description
            string paper = ReadIl2CppString(propsPtr + 0x30);  // Newspaper (NameInDatabase)
            int    idx   = *(int*)(propsPtr + 0x38);            // Index

            articles.Add((idx, paper, title, desc));
        }

        if (articles.Count == 0) { return ""; }

        articles.Sort((a, b) => a.idx.CompareTo(b.idx));

        int count = articles.Count;

        // Rotating window: advance a cursor a little each read so successive reads surface
        // fresh articles and, over the turn, cycle through the whole set. The window is NOT
        // keyed to the turn number — a turn spans many conversations/events and the papers
        // update throughout, so a turn-derived offset would freeze the same articles for the
        // entire turn. Reset the cursor only when the turn changes (new turn = a fresh set of
        // papers, start from the top).
        if (currentTurn != _newsTurn)
        {
            _newsTurn   = currentTurn;
            _newsOffset = 0;
        }
        _newsOffset %= count;  // articles can be added/removed mid-turn — keep cursor in range

        int take = Math.Min(ArticlesPerRead, count);
        var usedPapers = new HashSet<string>(System.StringComparer.OrdinalIgnoreCase);

        var lines = new List<string>(take);
        int scanned = 0;
        while (scanned < count && lines.Count < take)
        {
            var (_, paper, title, desc) = articles[(_newsOffset + scanned) % count];
            scanned++;
            // One article per newspaper per read; a skipped one comes around on a later read.
            if (!string.IsNullOrWhiteSpace(paper) && !usedPapers.Add(paper)) { continue; }
            // Format the raw NameInDatabase (e.g. "SharedNewspaper_Geopolitico") into a readable
            // name ("Geopolitico") so the AI doesn't see the internal database key.
            string prefix = string.IsNullOrWhiteSpace(paper) ? "" : $"[{FormatPaperName(paper)}] ";
            lines.Add($"{prefix}\"{title}\" — {desc}");
        }

        // Advance the cursor past everything scanned this read so the next read continues
        // forward through the list (wrapping at the end) — full coverage, no fixed subset.
        _newsOffset = (_newsOffset + scanned) % count;

        return string.Join("\n", lines);
    }

    // Turns a newspaper's NameInDatabase into a human-readable name:
    // strip everything up to and including the first '_', then split CamelCase into words.
    // "SharedNewspaper_Geopolitico" → "Geopolitico"; "Newspaper_LachavenTimes" → "Lachaven Times".
    private static string FormatPaperName(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) { return raw; }
        int us = raw.IndexOf('_');
        string name = us >= 0 ? raw.Substring(us + 1) : raw;

        var sb = new System.Text.StringBuilder(name.Length + 4);
        for (int i = 0; i < name.Length; i++)
        {
            if (i > 0 && char.IsLower(name[i - 1]) && char.IsUpper(name[i])) { sb.Append(' '); }
            sb.Append(name[i]);
        }
        return sb.ToString();
    }

    // ── Report reading ────────────────────────────────────────────────────────

    private const int ReportMaxChars = 250;

    // Reports buffered since the last checkpoint. Dismissed at checkpoint boundary
    // so they clear from the game UI after the conversation that consumed them ends.
    private static readonly List<ReportData> _pendingDismissal = new();

    private static unsafe string ReadReports(GameFlowManager gfm, int currentTurn)
    {
        if (gfm == null || currentTurn <= 0) { return ""; }

        nint listPtr = *(nint*)(gfm.Pointer + 0x78);
        if (listPtr == 0) { return ""; }

        var activeReports = new Il2CppSystem.Collections.Generic.List<ReportData>(listPtr);
        var reports = new List<(string title, string desc)>();

        for (int i = 0; i < activeReports.Count; i++)
        {
            var report = activeReports[i];
            if (report == null || !report.IsEnabled) { continue; }
            if (report.TurnNo != currentTurn) { continue; }

            nint propsPtr = *(nint*)(report.Pointer + 0x38);
            if (propsPtr == 0) { continue; }

            string title = ReadIl2CppString(propsPtr + 0x20);
            if (string.IsNullOrWhiteSpace(title)) { continue; }

            string desc = TruncateAtSentence(ReadIl2CppString(propsPtr + 0x28), ReportMaxChars);

            // Track for deferred dismissal at checkpoint boundary (avoid duplicates).
            if (!_pendingDismissal.Contains(report)) { _pendingDismissal.Add(report); }
            reports.Add((title, desc));
        }

        if (reports.Count == 0) { return ""; }

        var lines = new List<string>(reports.Count);
        foreach (var (title, desc) in reports)
        {
            lines.Add(string.IsNullOrWhiteSpace(desc) ? title : $"{title} — {desc}");
        }

        return string.Join("\n", lines);
    }

    // Called from CheckpointPatch — dismisses buffered reports from the game UI after
    // the conversation that consumed them ends, then clears the buffer.
    public static unsafe void DismissBufferedReports(GameFlowManager gfm)
    {
        if (_pendingDismissal.Count == 0 || gfm == null) { return; }

        nint listPtr = *(nint*)(gfm.Pointer + 0x78);
        if (listPtr == 0) { _pendingDismissal.Clear(); return; }

        var activeReports = new Il2CppSystem.Collections.Generic.List<ReportData>(listPtr);
        for (int d = _pendingDismissal.Count - 1; d >= 0; d--)
        {
            _pendingDismissal[d].IsDone = true;
            activeReports.Remove(_pendingDismissal[d]);
        }
        _pendingDismissal.Clear();
        Plugin.Log.LogInfo("[GameStateReader] Dismissed buffered reports at checkpoint.");
    }

    private static string TruncateAtSentence(string text, int maxChars)
    {
        if (string.IsNullOrEmpty(text) || text.Length <= maxChars) { return text; }
        for (int i = maxChars - 1; i >= 0; i--)
        {
            if (text[i] == '.' || text[i] == '!' || text[i] == '?') { return text.Substring(0, i + 1); }
        }
        return text.Substring(0, maxChars) + "…";
    }

    // ── Shared pointer helpers ─────────────────────────────────────────────────

    private static unsafe string ReadStatName(TemplateHUDStat stat)
    {
        nint dataPtr  = *(nint*)(stat.Pointer + 0xB0);  // currentHUDStatData
        if (dataPtr  == 0) { return ""; }
        nint propsPtr = *(nint*)(dataPtr + 0x38);        // HUDStatProperties
        if (propsPtr == 0) { return ""; }
        return ReadIl2CppString(propsPtr + 0x10);        // Title
    }

    private static unsafe string ReadTextStatName(TemplateHUDTextStat stat)
    {
        nint dataPtr  = *(nint*)(stat.Pointer + 0x70);  // currentHUDTextStatData
        if (dataPtr  == 0) { return ""; }
        nint propsPtr = *(nint*)(dataPtr + 0x38);        // HUDTextStatProperties
        if (propsPtr == 0) { return ""; }
        return ReadIl2CppString(propsPtr + 0x10);        // Title
    }

    private static unsafe string ReadTMP(nint fieldAddress)
    {
        nint ptr = *(nint*)fieldAddress;
        if (ptr == 0) { return ""; }
        return new TMPro.TextMeshProUGUI((System.IntPtr)ptr).text ?? "";
    }

    private static unsafe string ReadIl2CppString(nint fieldAddress)
    {
        nint ptr = *(nint*)fieldAddress;
        if (ptr == 0) { return ""; }
        return new Il2CppSystem.String((System.IntPtr)ptr);
    }
}
