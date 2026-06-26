using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using PixelCrushers.DialogueSystem;
using UnityEngine;

namespace ShadowPresident;

public class DialogueDriver : MonoBehaviour
{
    public DialogueDriver(IntPtr ptr) : base(ptr) { }

    private float _nextAdvanceTime = 0f;
    private ConversationHandler _handler;
    private CharacterCustomizationPanel _customizationPanel;
    private DecisionPanel _decisionPanel;
    private PrologueEpiloguePanel _prologueEpiloguePanel;
    private PagedDecisionPanel _pagedPanel;

    // Async AI decision state
    private List<Response> _pendingResponses;
    private Task<(int index, string reasoning)?> _aiTask;

    void Update()
    {
        if (!Plugin.AutomationEnabled) { return; }
        if (!DialogueManager.isConversationActive) { return; }

        if (_handler == null) { _handler = FindObjectOfType<ConversationHandler>(); }
        if (_handler == null || !_handler.IsInConversation()) { return; }

        unsafe
        {
            if (*(bool*)(_handler.Pointer + 0xB0))
            {
                HandleChoices();
                return;
            }
        }

        // Don't auto-advance narration while the PagedDecisionPanel is up. Read the panel's real
        // isShowing field (0x120) directly: the Show()/Hide() Harmony patches do not reliably fire
        // (observed isShowing=True while the patch flag stayed False across an entire paged
        // decision), so PagedDecisionPanelShowPatch.IsShowing cannot be trusted.
        if (_pagedPanel == null) { _pagedPanel = FindObjectOfType<PagedDecisionPanel>(); }
        if (_pagedPanel != null)
        {
            bool showing;
            unsafe { showing = *(bool*)(_pagedPanel.Pointer + 0x120); }
            if (showing) { return; }
        }

        if (_customizationPanel == null) { _customizationPanel = FindObjectOfType<CharacterCustomizationPanel>(); }
        if (_customizationPanel != null && _customizationPanel.IsShowing()) { return; }

        // DecisionPanel can appear mid-conversation (e.g. lobbying during assembly).
        if (_decisionPanel == null) { _decisionPanel = FindObjectOfType<DecisionPanel>(); }
        if (_decisionPanel != null && _decisionPanel.IsShowing()) { return; }

        // PrologueEpiloguePanel uses its own dialogueUi — let PrologueEpilogueDriver handle it.
        if (_prologueEpiloguePanel == null) { _prologueEpiloguePanel = FindObjectOfType<PrologueEpiloguePanel>(); }
        if (_prologueEpiloguePanel != null && _prologueEpiloguePanel.IsShowing()) { return; }

        // No choices showing — auto-advance if timer allows
        if (Time.time < _nextAdvanceTime) { return; }
        _handler.OnContinue();
        _nextAdvanceTime = Time.time + 0.15f;
    }

    private void HandleChoices()
    {
        // AI task is running — poll for completion
        if (_aiTask != null)
        {
            if (!_aiTask.IsCompleted) { return; }
            ExecuteAIChoice();
            return;
        }

        // First frame seeing these choices — gather responses and decide
        var state = DialogueManager.currentConversationState;
        if (state == null || !state.hasPCResponses) { return; }

        var responses = state.pcResponses;
        if (responses == null) { return; }

        var enabled = new List<Response>();
        for (int i = 0; i < responses.Length; i++)
        {
            var r = responses[i];
            if (r != null && r.enabled) { enabled.Add(r); }
        }
        if (enabled.Count == 0) { return; }

        // Single choice — select immediately, no AI needed; not a meaningful decision
        if (enabled.Count == 1)
        {
            SelectResponse(enabled[0], "", isRealChoice: false);
            return;
        }

        // Random mode — pick immediately
        if (!Plugin.UseAIServer.Value)
        {
            SelectResponse(enabled[UnityEngine.Random.Range(0, enabled.Count)], "");
            return;
        }

        // AI mode — extract text on main thread, then kick off background task
        var choices = new List<(int, string)>(enabled.Count);
        for (int i = 0; i < enabled.Count; i++)
            choices.Add((i, ConversationLinePatch.StripTags(enabled[i].formattedText?.text ?? "")));

        _pendingResponses = enabled;
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
            Plugin.Log.LogWarning("[DialogueDriver] AI server unreachable — pausing automation.");
            AIOverlay.ShowError("Cannot reach AI server.");
            Plugin.AutomationEnabled = false;
            Plugin.SafeStopPending = false;
            return;
        }

        int idx = Math.Max(0, Math.Min(result.Value.index, pending.Count - 1));
        SelectResponse(pending[idx], result.Value.reasoning);
    }

    private void SelectResponse(Response r, string reasoning, bool isRealChoice = true)
    {
        string text = ConversationLinePatch.StripTags(r.formattedText?.text ?? "");
        Plugin.Log.LogInfo($"[DialogueDriver] Picking: {text}");
        AIOverlay.ShowReasoning(reasoning);
        if (!string.IsNullOrWhiteSpace(text))
            AIClient.AddContext(isRealChoice ? "[CHOICE]" : "[AUTO]", text);
        _handler.OnResponseClick(r);
        _nextAdvanceTime = Time.time + 0.15f;
    }
}
