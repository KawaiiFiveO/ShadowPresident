using System;
using System.Collections.Generic;
using UnityEngine;

namespace ShadowPresident;

// Reads the game's Journal — the permanent factual spine of the playthrough. Each entry is a
// terse, turn-stamped fact the game itself considers durable (treaties signed, crises resolved,
// reforms passed). Distinct from memory.txt (subjective in-conversation interpretation): the
// ledger is facts, memory is judgment. Cached to AIClient.CurrentJournal for later injection as
// a system-prompt block (roadmap #4).
//
// Source: EntityDataManager.JournalEntriesData (static List<JournalEntryData>).
//   JournalEntryData.IsEnabled (getter)            — only enabled entries are live
//   JournalEntryData.JournalEntryProperties  +0x38 — sub-object
//     JournalEntryProperties.TurnNo           +0x10
//     JournalEntryProperties.Description       +0x20
public class JournalReader : MonoBehaviour
{
    public JournalReader(IntPtr ptr) : base(ptr) { }

    private float _nextReadTime = 0f;
    private const float ReadInterval = 30f;

    void Update()
    {
        float now = Time.time;
        if (now < _nextReadTime) { return; }
        _nextReadTime = now + ReadInterval;
        ReadJournal();
    }

    private unsafe void ReadJournal()
    {
        var entries = EntityDataManager.JournalEntriesData;
        if (entries == null) { return; }

        var facts = new List<(int turn, int index, string text)>();

        for (int i = 0; i < entries.Count; i++)
        {
            var entry = entries[i];
            if (entry == null || !entry.IsEnabled) { continue; }

            nint propsPtr = *(nint*)(entry.Pointer + 0x38);  // JournalEntryProperties
            if (propsPtr == 0) { continue; }

            int turn = *(int*)(propsPtr + 0x10);  // TurnNo
            string desc = ConversationLinePatch.StripTags(ReadIl2CppString(propsPtr + 0x20));  // Description
            if (string.IsNullOrWhiteSpace(desc)) { continue; }

            facts.Add((turn, i, desc));
        }

        if (facts.Count == 0)
        {
            if (AIClient.CurrentJournal.Length != 0)
            {
                AIClient.CurrentJournal = "";
            }
            return;
        }

        // Stable order: by turn (ascending), then by original list index (descending)
        facts.Sort((a, b) => a.turn != b.turn ? a.turn.CompareTo(b.turn) : b.index.CompareTo(a.index));

        var lines = new List<string>(facts.Count);
        foreach (var (turn, _, text) in facts)
        {
            lines.Add($"T{turn:D2}: {text}");
        }

        string result = string.Join("\n", lines);
        bool changed = result != AIClient.CurrentJournal;
        AIClient.CurrentJournal = result;

        // The full ledger is long and is visible in the browser panel; only note size changes
        // in the console to avoid spamming it every read.
        if (changed)
        {
            Plugin.Log.LogInfo($"[JournalReader] {facts.Count} entries (updated).");
        }
    }

    private static unsafe string ReadIl2CppString(nint fieldAddress)
    {
        nint ptr = *(nint*)fieldAddress;
        if (ptr == 0) { return ""; }
        return new Il2CppSystem.String((System.IntPtr)ptr);
    }
}
