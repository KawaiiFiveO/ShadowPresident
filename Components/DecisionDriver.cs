using System;
using System.Collections.Generic;
using System.Reflection;
using System.Threading.Tasks;
using HarmonyLib;
using PixelCrushers.DialogueSystem;
using UnityEngine;

namespace ShadowPresident;

public class DecisionDriver : MonoBehaviour
{
    public DecisionDriver(IntPtr ptr) : base(ptr) { }

    private const float PostConversationDelay = 2f;
    private const float FragmentCooldown      = 60f; // prevent re-triggering the same fragment

    private float _nextActionTime = 0f;
    private string _lastFragmentName = "";
    private float  _lastFragmentTime = -999f;
    private bool _wasInConversation = false;
    private Task<(int index, string reasoning)?> _decisionAiTask;
    private int _decisionButtonCount;
    private DecisionPanel _decisionPanel;
    private ConversationPanel _conversationPanel;
    private BillPanel _billPanel;
    private ContinueButtonPanel _continueButtonPanel;
    private GameFlowManager _gfm;
    private MethodInfo _forceStartConversation;
    private MethodInfo _forceStartDecision;
    private MethodInfo _forceStartBill;

    void Update()
    {
        if (!Plugin.AutomationEnabled) { _wasInConversation = false; return; }

        bool inConversation = DialogueManager.isConversationActive;
        if (_wasInConversation && !inConversation)
        {
            Plugin.Log.LogInfo($"[DecisionDriver] Conversation ended — waiting {PostConversationDelay}s for autosave.");
            _nextActionTime = Time.time + PostConversationDelay;
        }
        _wasInConversation = inConversation;

        if (_decisionPanel == null) { _decisionPanel = FindObjectOfType<DecisionPanel>(); }
        if (_conversationPanel == null) { _conversationPanel = FindObjectOfType<ConversationPanel>(); }
        if (_billPanel == null) { _billPanel = FindObjectOfType<BillPanel>(); }
        if (_continueButtonPanel == null) { _continueButtonPanel = FindObjectOfType<ContinueButtonPanel>(); }
        if (_gfm == null) { _gfm = FindObjectOfType<GameFlowManager>(); }
        if (_gfm != null)
        {
            AIClient.CurrentTurn = _gfm.CurrentTurnNo;
            AIClient.CurrentStep = _gfm.CurrentStepNo;
        }

        // DecisionPanel can appear while a conversation is paused (e.g. lobbying during assembly).
        // Check it before the conversation guard so it isn't skipped.
        if (_decisionPanel != null && _decisionPanel.IsShowing())
        {
            if (Time.time >= _nextActionTime) { PickRandomDecisionOption(); }
            return;
        }

        if (inConversation) { return; }
        if (Time.time < _nextActionTime) { return; }

        if (_continueButtonPanel != null && _continueButtonPanel.IsShowing())
        {
            Plugin.Log.LogInfo("[DecisionDriver] Clicking turn Continue button.");
            _continueButtonPanel.OnContinueButtonClick();
            _nextActionTime = Time.time + 0.5f;
            return;
        }

        if (_gfm == null) { return; }

        // isStoryFragmentActive at 0x40 — don't trigger while one is already running
        unsafe
        {
            if (*(bool*)(_gfm.Pointer + 0x40)) { return; }
        }

        var pending = _gfm.GetEnabledNotDoneStoryFragments();
        if (pending == null || pending.Count == 0) { return; }

        int idx = UnityEngine.Random.Range(0, pending.Count);
        TriggerFragment(pending[idx]);
    }

    private void TriggerFragment(StoryFragmentData fragment)
    {
        // Update current fragment name for log context
        unsafe
        {
            nint namePtr = *(nint*)(fragment.Pointer + 0x18);
            if (namePtr != 0) { AIClient.CurrentFragment = new Il2CppSystem.String((System.IntPtr)namePtr); }
        }

        var convData = fragment.TryCast<ConversationData>();
        if (convData != null)
        {
            Plugin.Log.LogInfo($"[DecisionDriver] Starting conversation: {convData.NameInDatabase}");
            if (_conversationPanel == null) { Plugin.Log.LogWarning("[DecisionDriver] ConversationPanel not found."); return; }
            if (_forceStartConversation == null)
            {
                _forceStartConversation = AccessTools.Method(typeof(ConversationPanel), "ForceStartConversation");
                if (_forceStartConversation == null) { Plugin.Log.LogWarning("[DecisionDriver] ForceStartConversation method not found."); return; }
            }
            string name = convData.NameInDatabase;
            if (name == _lastFragmentName && Time.time - _lastFragmentTime < FragmentCooldown)
            {
                Plugin.Log.LogWarning($"[DecisionDriver] Skipping duplicate trigger of {name} ({Time.time - _lastFragmentTime:F0}s since last trigger).");
                return;
            }
            _lastFragmentName = name;
            _lastFragmentTime = Time.time;
            try { _forceStartConversation.Invoke(_conversationPanel, new object[] { name }); }
            catch (Exception ex) { Plugin.Log.LogError($"[DecisionDriver] ForceStartConversation failed: {ex.GetBaseException().Message}"); }
            _nextActionTime = Time.time + 1.0f;
            return;
        }

        var decData = fragment.TryCast<DecisionData>();
        if (decData != null)
        {
            Plugin.Log.LogInfo($"[DecisionDriver] Starting decision: {decData.NameInDatabase}");
            if (_decisionPanel == null) { Plugin.Log.LogWarning("[DecisionDriver] DecisionPanel not found."); return; }
            if (_forceStartDecision == null)
            {
                _forceStartDecision = AccessTools.Method(typeof(DecisionPanel), "ForceStartDecision");
                if (_forceStartDecision == null) { Plugin.Log.LogWarning("[DecisionDriver] ForceStartDecision method not found."); return; }
            }
            try { _forceStartDecision.Invoke(_decisionPanel, new object[] { decData.NameInDatabase }); }
            catch (Exception ex) { Plugin.Log.LogError($"[DecisionDriver] ForceStartDecision failed: {ex.GetBaseException().Message}"); }
            _nextActionTime = Time.time + 1.0f;
            return;
        }

        var billData = fragment.TryCast<BillData>();
        if (billData != null)
        {
            Plugin.Log.LogInfo($"[DecisionDriver] Starting bill: {billData.NameInDatabase}");
            if (_billPanel == null) { Plugin.Log.LogWarning("[DecisionDriver] BillPanel not found."); return; }
            if (_forceStartBill == null)
            {
                _forceStartBill = AccessTools.Method(typeof(BillPanel), "ForceStartBill");
                if (_forceStartBill == null) { Plugin.Log.LogWarning("[DecisionDriver] ForceStartBill method not found."); return; }
            }
            try { _forceStartBill.Invoke(_billPanel, new object[] { billData.NameInDatabase }); }
            catch (Exception ex) { Plugin.Log.LogError($"[DecisionDriver] ForceStartBill failed: {ex.GetBaseException().Message}"); }
            _nextActionTime = Time.time + 1.0f;
            return;
        }

        Plugin.Log.LogWarning($"[DecisionDriver] Unhandled fragment type, skipping.");
        _nextActionTime = Time.time + 1.0f;
    }

    private void PickRandomDecisionOption()
    {
        unsafe
        {
            nint listPtr = *(nint*)(_decisionPanel.Pointer + 0x60);
            if (listPtr == 0) { return; }

            var buttons = new Il2CppSystem.Collections.Generic.List<TemplateDecisionOptionButton>(listPtr);
            if (buttons.Count == 0) { return; }

            // AI task is running — poll
            if (_decisionAiTask != null)
            {
                if (!_decisionAiTask.IsCompleted) { return; }

                var result = _decisionAiTask.Result;
                _decisionAiTask = null;

                if (result == null)
                {
                    Plugin.Log.LogWarning("[DecisionDriver] AI server unreachable — pausing automation.");
                    AIOverlay.ShowError("Cannot reach AI server.");
                    Plugin.AutomationEnabled = false;
                    Plugin.SafeStopPending = false;
                    return;
                }

                int chosen = Math.Max(0, Math.Min(result.Value.index, _decisionButtonCount - 1));
                Plugin.Log.LogInfo($"[DecisionDriver] Picking decision option [{chosen + 1}/{buttons.Count}].");
                AIOverlay.ShowReasoning(result.Value.reasoning);
                nint chosenTextPtr = *(nint*)(buttons[chosen].Pointer + 0x20);
                if (chosenTextPtr != 0)
                {
                    string chosenText = ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)chosenTextPtr).text ?? "");
                    if (!string.IsNullOrWhiteSpace(chosenText)) { AIClient.AddContext("[CHOICE]", chosenText); }
                }
                buttons[chosen].OnClick();
                _nextActionTime = Time.time + 0.5f;
                return;
            }

            // Single-button decision panel — there is no real decision to make. Record the
            // event and the forced option as context (so the AI still sees the outcome) and
            // click it without calling the AI. Mirrors the single-response rule in DialogueDriver.
            if (buttons.Count == 1)
            {
                string title = "";
                nint titlePtr = *(nint*)(_decisionPanel.Pointer + 0x28);
                if (titlePtr != 0) { title = ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)titlePtr).text ?? ""); }
                string desc = "";
                nint descPtr = *(nint*)(_decisionPanel.Pointer + 0x30);
                if (descPtr != 0) { desc = ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)descPtr).text ?? ""); }
                if (!string.IsNullOrWhiteSpace(title))
                {
                    string contextLabel = string.IsNullOrWhiteSpace(desc) ? title : $"{title}: {desc}";
                    AIClient.AddContext("Event", contextLabel);
                }

                string onlyText = "";
                nint onlyTextPtr = *(nint*)(buttons[0].Pointer + 0x20);
                if (onlyTextPtr != 0) { onlyText = ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)onlyTextPtr).text ?? ""); }
                if (!string.IsNullOrWhiteSpace(onlyText)) { AIClient.AddContext("[AUTO]", onlyText); }

                Plugin.Log.LogInfo($"[DecisionDriver] Single-option decision panel — auto-selecting: {onlyText}");
                buttons[0].OnClick();
                _nextActionTime = Time.time + 0.5f;
                return;
            }

            if (!Plugin.UseAIServer.Value)
            {
                int chosen = UnityEngine.Random.Range(0, buttons.Count);
                Plugin.Log.LogInfo($"[DecisionDriver] Picking decision option [{chosen + 1}/{buttons.Count}].");
                buttons[chosen].OnClick();
                _nextActionTime = Time.time + 0.5f;
                return;
            }

            // Extract text on main thread, then start background task
            string panelTitle = "";
            nint titleTmpPtr = *(nint*)(_decisionPanel.Pointer + 0x28);
            if (titleTmpPtr != 0) { panelTitle = ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)titleTmpPtr).text ?? ""); }
            string panelDesc = "";
            nint descTmpPtr = *(nint*)(_decisionPanel.Pointer + 0x30);
            if (descTmpPtr != 0) { panelDesc = ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)descTmpPtr).text ?? ""); }

            var choices = new List<(int, string)>(buttons.Count);
            for (int i = 0; i < buttons.Count; i++)
            {
                string text = "";
                nint textPtr = *(nint*)(buttons[i].Pointer + 0x20);
                if (textPtr != 0)
                    text = ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)textPtr).text ?? "");
                choices.Add((i, text));
            }
            _decisionButtonCount = buttons.Count;
            if (!string.IsNullOrWhiteSpace(panelTitle))
            {
                string contextLabel = string.IsNullOrWhiteSpace(panelDesc) ? panelTitle : $"{panelTitle}: {panelDesc}";
                AIClient.AddContext("Event", contextLabel);
            }
            GameState.EnsureRead();
            AIOverlay.ShowThinking();
            _decisionAiTask = Task.Run(() => AIClient.RequestDecision("decision_panel", choices));
        }
    }
}
