using System.Collections.Generic;
using System.Threading.Tasks;
using HarmonyLib;
using PixelCrushers.DialogueSystem;
using UnityEngine;

namespace ShadowPresident;

// Log the response the human clicked in normal dialogue.
// Prefix fires before OnResponseClick so DialogueManager.currentConversationState is still valid.
[HarmonyPatch(typeof(ConversationHandler), nameof(ConversationHandler.OnResponseClick))]
public class HumanResponseClickPatch
{
    static void Prefix(Response response)
    {
        if (Plugin.AutomationEnabled) { return; }
        if (!Plugin.UseAIServer.Value) { return; }

        var state = DialogueManager.currentConversationState;
        if (state == null || !state.hasPCResponses) { return; }

        var responses = state.pcResponses;
        if (responses == null) { return; }

        var choices = new List<(int, string)>();
        int chosenIdx = -1;
        int enabledIdx = 0;
        for (int i = 0; i < responses.Length; i++)
        {
            var r = responses[i];
            if (r == null || !r.enabled) { continue; }
            string text = ConversationLinePatch.StripTags(r.formattedText?.text ?? "");
            if (r.Pointer == response.Pointer) { chosenIdx = enabledIdx; }
            choices.Add((enabledIdx, text));
            enabledIdx++;
        }

        if (chosenIdx < 0) { return; }

        // Add the chosen text to context so the AI sees what was said.
        string chosenText = choices[chosenIdx].Item2;
        if (!string.IsNullOrWhiteSpace(chosenText))
            AIClient.AddContext("[CHOICE]", chosenText);

        var choicesCopy = choices;
        int idx = chosenIdx;
        Task.Run(() => AIClient.LogHumanChoice("dialogue", choicesCopy, idx));
    }
}

// Log when the human signs a bill.
[HarmonyPatch(typeof(BillPanel), nameof(BillPanel.SignBill))]
public class HumanSignBillPatch
{
    static void Prefix(BillPanel __instance)
    {
        if (Plugin.AutomationEnabled) { return; }
        if (!Plugin.UseAIServer.Value) { return; }

        string title = ReadTMPText(__instance.Pointer + 0x28);
        var choices = new List<(int, string)> { (0, $"Sign: {title}"), (1, $"Veto: {title}") };
        AIClient.AddContext("[CHOICE]", $"Sign: {title}");
        Task.Run(() => AIClient.LogHumanChoice("bill", choices, 0));
    }

    private static unsafe string ReadTMPText(nint fieldAddress)
    {
        nint ptr = *(nint*)fieldAddress;
        if (ptr == 0) { return ""; }
        return ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((System.IntPtr)ptr).text ?? "");
    }
}

// Log when the human vetoes a bill.
[HarmonyPatch(typeof(BillPanel), nameof(BillPanel.VetoBill))]
public class HumanVetoBillPatch
{
    static void Prefix(BillPanel __instance)
    {
        if (Plugin.AutomationEnabled) { return; }
        if (!Plugin.UseAIServer.Value) { return; }

        string title = ReadTMPText(__instance.Pointer + 0x28);
        var choices = new List<(int, string)> { (0, $"Sign: {title}"), (1, $"Veto: {title}") };
        AIClient.AddContext("[CHOICE]", $"Veto: {title}");
        Task.Run(() => AIClient.LogHumanChoice("bill", choices, 1));
    }

    private static unsafe string ReadTMPText(nint fieldAddress)
    {
        nint ptr = *(nint*)fieldAddress;
        if (ptr == 0) { return ""; }
        return ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((System.IntPtr)ptr).text ?? "");
    }
}

// Log when the human picks a map event from the DecisionPanel.
// The button stores its own index at 0x38, so we don't need to search the list to find it.
[HarmonyPatch(typeof(TemplateDecisionOptionButton), nameof(TemplateDecisionOptionButton.OnClick))]
public class HumanDecisionButtonPatch
{
    static void Prefix(TemplateDecisionOptionButton __instance)
    {
        if (Plugin.AutomationEnabled) { return; }
        if (!Plugin.UseAIServer.Value) { return; }

        var panel = Object.FindObjectOfType<DecisionPanel>();
        if (panel == null) { return; }

        var choices = new List<(int, string)>();
        int chosenIdx;

        unsafe
        {
            chosenIdx = *(int*)(__instance.Pointer + 0x38);

            nint listPtr = *(nint*)(panel.Pointer + 0x60);
            if (listPtr == 0) { return; }

            var buttons = new Il2CppSystem.Collections.Generic.List<TemplateDecisionOptionButton>(listPtr);
            for (int i = 0; i < buttons.Count; i++)
            {
                var btn = buttons[i];
                string text = "";
                if (btn != null)
                {
                    nint textPtr = *(nint*)(btn.Pointer + 0x20);
                    if (textPtr != 0) { text = ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((System.IntPtr)textPtr).text ?? ""); }
                }
                choices.Add((i, text));
            }
        }

        if (choices.Count == 0) { return; }

        if (chosenIdx < choices.Count)
        {
            string chosenText = choices[chosenIdx].Item2;
            if (!string.IsNullOrWhiteSpace(chosenText))
                AIClient.AddContext("[CHOICE]", chosenText);
        }

        var choicesCopy = choices;
        int idx = chosenIdx;
        Task.Run(() => AIClient.LogHumanChoice("decision_panel", choicesCopy, idx));
    }
}
