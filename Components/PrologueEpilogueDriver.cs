using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using PixelCrushers.DialogueSystem;
using UnityEngine;

namespace ShadowPresident;

// The epilogue (and prologue) run a PCDS conversation through PrologueEpiloguePanel.dialogueUi
// (offset 0x30), not through ConversationHandler. SetupEpilogue() calls
// Panels.SwitchDialogueUI(dialogueUi), deactivating ConversationPanel and its ConversationHandler.
// OnEpilogueFinish() is called by the PCDS sequencer at the end — we must NOT call it ourselves.
public class PrologueEpilogueDriver : MonoBehaviour
{
    public PrologueEpilogueDriver(IntPtr ptr) : base(ptr) { }

    private float _nextAdvanceTime = 0f;
    private PrologueEpiloguePanel _panel;
    private ConversationHandler _handler;
    private string _lastSubtitleText = "";

    private List<Response> _pendingResponses;
    private Task<(int index, string reasoning)?> _aiTask;

    void Update()
    {
        if (!Plugin.AutomationEnabled) { return; }

        if (_panel == null) { _panel = FindObjectOfType<PrologueEpiloguePanel>(); }

        if (_panel == null || !_panel.IsShowing()) { return; }
        if (!DialogueManager.isConversationActive) { return; }

        if (_handler == null) { _handler = FindObjectOfType<ConversationHandler>(true); }

        var state = DialogueManager.currentConversationState;
        if (state != null && state.hasPCResponses)
        {
            HandleChoices(state);
            return;
        }

        if (Time.time < _nextAdvanceTime) { return; }
        AdvanceDialogue();
    }

    private void HandleChoices(ConversationState state)
    {
        if (_aiTask != null)
        {
            if (!_aiTask.IsCompleted) { return; }
            ExecuteAIChoice();
            return;
        }

        // Capture the subtitle currently visible alongside the choices (the question/prompt)
        // before recording the choice itself, so context stays chronological.
        unsafe
        {
            nint uiPtr = *(nint*)(_panel.Pointer + 0x30);
            if (uiPtr != 0)
            {
                string subtitle = ReadSubtitleText(uiPtr);
                if (!string.IsNullOrWhiteSpace(subtitle) && subtitle != _lastSubtitleText)
                {
                    _lastSubtitleText = subtitle;
                    string stripped = ConversationLinePatch.StripTags(subtitle);
                    if (!string.IsNullOrWhiteSpace(stripped))
                    {
                        Plugin.Log.LogInfo($"[Dialogue] Narrator: {stripped}");
                        AIClient.AddContext("Narrator", stripped);
                    }
                }
            }
        }

        var responses = state.pcResponses;
        if (responses == null) { return; }

        var enabled = new List<Response>();
        for (int i = 0; i < responses.Length; i++)
        {
            var r = responses[i];
            if (r != null && r.enabled) { enabled.Add(r); }
        }
        if (enabled.Count == 0) { return; }

        if (enabled.Count == 1)
        {
            SelectResponse(enabled[0], "", isRealChoice: false);
            return;
        }

        if (!Plugin.UseAIServer.Value)
        {
            SelectResponse(enabled[UnityEngine.Random.Range(0, enabled.Count)], "");
            return;
        }

        var choices = new List<(int, string)>(enabled.Count);
        for (int i = 0; i < enabled.Count; i++)
            choices.Add((i, ConversationLinePatch.StripTags(enabled[i].formattedText?.text ?? "")));

        _pendingResponses = enabled;
        AIClient.CurrentPhase = _panel.State == PrologueEpiloguePanel.PrologueEpilogueState.Epilogue ? "epilogue" : "prologue";
        AIOverlay.ShowThinking();
        _aiTask = Task.Run(() => AIClient.RequestDecision("dialogue", choices));
    }

    private void ExecuteAIChoice()
    {
        var result = _aiTask.Result;
        _aiTask = null;

        var pending = _pendingResponses;
        _pendingResponses = null;

        if (result == null)
        {
            Plugin.Log.LogWarning("[PrologueEpilogueDriver] AI server unreachable — pausing automation.");
            AIOverlay.ShowError("Cannot reach AI server.");
            Plugin.AutomationEnabled = false;
            Plugin.SafeStopPending = false;
            return;
        }

        AIClient.CurrentPhase = "main";
        int idx = Math.Max(0, Math.Min(result.Value.index, pending.Count - 1));
        SelectResponse(pending[idx], result.Value.reasoning);
    }

    private void SelectResponse(Response chosen, string reasoning, bool isRealChoice = true)
    {
        string text = ConversationLinePatch.StripTags(chosen.formattedText?.text ?? "");
        Plugin.Log.LogInfo($"[PrologueEpilogueDriver] Picking: {text}");
        AIOverlay.ShowReasoning(reasoning);
        // Record both real AI picks ([CHOICE]) and forced single-option picks ([AUTO]) — same as
        // DialogueDriver. Previously the [AUTO] case was dropped, so single-choice prologue lines
        // never reached the context/transcript/JSONL. [AUTO] is context only; it doesn't trigger
        // memory (the server keys memory off [CHOICE]).
        if (!string.IsNullOrWhiteSpace(text))
            AIClient.AddContext(isRealChoice ? "[CHOICE]" : "[AUTO]", text);
        _nextAdvanceTime = Time.time + 0.5f;

        if (_handler == null) { return; }

        try
        {
            // responsesAreShown (0xB0) is false because ConversationHandler is inactive.
            // OnResponseClick bails if it's false — force it true. It clears itself after.
            unsafe { *(bool*)(_handler.Pointer + 0xB0) = true; }
            _handler.OnResponseClick(chosen);
        }
        catch (Exception ex)
        {
            Plugin.Log.LogError($"[PrologueEpilogueDriver] OnResponseClick threw: {ex.GetBaseException().Message}");
        }
    }

    private void AdvanceDialogue()
    {
        // Read PrologueEpiloguePanel.dialogueUi directly at offset 0x30 —
        // standardDialogueUI may still point to the old ConversationPanel UI after SwitchDialogueUI
        unsafe
        {
            nint uiPtr = *(nint*)(_panel.Pointer + 0x30);
            if (uiPtr == 0) { return; }

            // Capture the currently-visible subtitle text before advancing.
            // Chain: StandardDialogueUI.conversationUIElements (+0x38)
            //      → StandardUIDialogueControls.subtitlePanels (+0x20) [array]
            //      → panels[0] (+0x20 into array header)
            //      → StandardUISubtitlePanel.subtitleText (+0xC0) [UITextField]
            //      → UITextField.m_textMeshProUGUI (+0x18)
            string text = ReadSubtitleText(uiPtr);
            if (!string.IsNullOrWhiteSpace(text) && text != _lastSubtitleText)
            {
                _lastSubtitleText = text;
                string stripped = ConversationLinePatch.StripTags(text);
                if (!string.IsNullOrWhiteSpace(stripped))
                {
                    Plugin.Log.LogInfo($"[Dialogue] Narrator: {stripped}");
                    AIClient.AddContext("Narrator", stripped);
                }
            }

            new StandardDialogueUI((IntPtr)uiPtr).OnContinueConversation();
            _nextAdvanceTime = Time.time + 0.3f;
        }
    }

    private static unsafe string ReadSubtitleText(nint uiPtr)
    {
        try
        {
            nint controlsPtr = *(nint*)(uiPtr + 0x38);           // conversationUIElements
            if (controlsPtr == 0) { return ""; }
            nint panelsArrayPtr = *(nint*)(controlsPtr + 0x20); // subtitlePanels (array)
            if (panelsArrayPtr == 0) { return ""; }
            nint panel0Ptr = *(nint*)(panelsArrayPtr + 0x20);   // panels[0] (skip IL2Cpp array header)
            if (panel0Ptr == 0) { return ""; }

            // Read m_currentSubtitle (Subtitle) at +0x130 — set by PCDS when text is displayed.
            // Using the Subtitle object directly avoids the UITextField/TMP/legacy-Text ambiguity.
            nint subtitlePtr = *(nint*)(panel0Ptr + 0x130);
            if (subtitlePtr == 0) { return ""; }

            var subtitle = new Subtitle((IntPtr)subtitlePtr);
            return subtitle.formattedText?.text ?? "";
        }
        catch (Exception ex)
        {
            Plugin.Log.LogWarning($"[PrologueEpilogueDriver] ReadSubtitleText failed: {ex.GetBaseException().Message}");
            return "";
        }
    }
}
