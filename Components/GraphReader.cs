using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

namespace ShadowPresident;

// Reads the game's economy/approval graph panels — the same historical charts a human player can
// open. A fair stand-in for the hidden win-condition stats: the AI sees the trajectory of each
// tracked variable, not just its current value. Cached to AIClient.CurrentEconomy for later
// injection at the tail of the user block (roadmap #5).
//
// Source: EntityDataManager.GraphPanelData (static List<GraphPanelData>).
//   GraphPanelData.IsWIP                +0x28 — skip work-in-progress panels
//   GraphPanelData.GraphPanelProperties +0x38 — sub-object
//     GraphPanelProperties.Title         +0x18
//   GraphPanelData.HistoricalData              — List<int> series (refreshed per story fragment)
//
// The game refreshes HistoricalData in FinishStoryFragmentCoroutine (per-fragment cadence), so a
// read shortly after a checkpoint reflects the latest point. We poll on a timer here purely so the
// console log shows the series during testing.
public class GraphReader : MonoBehaviour
{
    public GraphReader(IntPtr ptr) : base(ptr) { }

    private float _nextReadTime = 0f;
    private const float ReadInterval = 30f;

    // Number of trailing points to show per series — enough to read a trend without bloat.
    private const int TrailPoints = 6;

    void Update()
    {
        float now = Time.time;
        if (now < _nextReadTime) { return; }
        _nextReadTime = now + ReadInterval;
        ReadGraphs();
    }

    private unsafe void ReadGraphs()
    {
        var graphs = EntityDataManager.GraphPanelData;
        if (graphs == null) { return; }

        var lines = new List<string>();

        for (int i = 0; i < graphs.Count; i++)
        {
            var graph = graphs[i];
            if (graph == null) { continue; }
            if (*(byte*)(graph.Pointer + 0x28) != 0) { continue; }  // IsWIP

            nint propsPtr = *(nint*)(graph.Pointer + 0x38);  // GraphPanelProperties
            if (propsPtr == 0) { continue; }

            string title = ConversationLinePatch.StripTags(ReadIl2CppString(propsPtr + 0x18));  // Title
            if (string.IsNullOrWhiteSpace(title)) { continue; }

            var data = graph.HistoricalData;
            string series = FormatSeries(data);

            lines.Add($"{title}: {series}");
        }

        if (lines.Count == 0)
        {
            Plugin.Log.LogInfo("[GraphReader] No graph panels with data yet.");
            return;
        }

        string result = string.Join("\n", lines);
        bool changed = result != AIClient.CurrentEconomy;
        AIClient.CurrentEconomy = result;

        Plugin.Log.LogInfo($"[GraphReader] {lines.Count} graphs{(changed ? " (changed)" : "")}:");
        foreach (var line in lines)
        {
            Plugin.Log.LogInfo($"[GraphReader]   {line}");
        }
    }

    // "12 -> 15 -> 14 (now 14, chg -1)" — last TrailPoints values plus the latest reading and the
    // change from the previous point. ASCII only so it renders in the BepInEx console.
    private static string FormatSeries(Il2CppSystem.Collections.Generic.List<int> data)
    {
        if (data == null || data.Count == 0) { return "no data"; }

        int n = data.Count;
        int take = Math.Min(TrailPoints, n);

        var sb = new StringBuilder();
        for (int i = n - take; i < n; i++)
        {
            if (i > n - take) { sb.Append(" -> "); }
            sb.Append(data[i]);
        }

        int last = data[n - 1];
        int delta = n >= 2 ? last - data[n - 2] : 0;
        string chg = delta > 0 ? $"+{delta}" : delta.ToString();
        sb.Append($" (now {last}, chg {chg})");
        return sb.ToString();
    }

    private static unsafe string ReadIl2CppString(nint fieldAddress)
    {
        nint ptr = *(nint*)fieldAddress;
        if (ptr == 0) { return ""; }
        return new Il2CppSystem.String((System.IntPtr)ptr);
    }
}
