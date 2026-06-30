using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using UnityEngine;

namespace ShadowPresident;

public class DecreePanelDriver : MonoBehaviour
{
    public DecreePanelDriver(IntPtr ptr) : base(ptr) { }

    private float _nextActionTime = 0f;
    private OneTimeDecreesPanel _oneTimePanel;
    private ReusableDecreesPanel _reusablePanel;

    // Shared AI task state (used by both one-time and reusable panels)
    private enum DecreeState { Idle, WaitingForAI, OpeningDetails, Signing, Closing }
    private DecreeState _state = DecreeState.Idle;
    private Task<(int index, string reasoning)?> _aiTask;
    private List<DecreeData> _availableDecrees;
    private DecreeData _chosenDecree;

    void Update()
    {
        if (!Plugin.AutomationEnabled) { return; }
        if (Time.time < _nextActionTime) { return; }

        if (_oneTimePanel == null) { _oneTimePanel = FindObjectOfType<OneTimeDecreesPanel>(); }
        if (_reusablePanel == null) { _reusablePanel = FindObjectOfType<ReusableDecreesPanel>(); }

        if (_oneTimePanel != null && _oneTimePanel.IsShowing())
        {
            HandleOneTimePanel();
            return;
        }

        if (_reusablePanel != null && _reusablePanel.IsShowing())
        {
            HandleReusablePanel();
            return;
        }

        // Panel closed — reset
        if (_state != DecreeState.Idle) { ResetState(); }
    }

    private void HandleOneTimePanel()
    {
        switch (_state)
        {
            case DecreeState.Idle:
                ReadOneTimeDecreesAndAsk();
                break;

            case DecreeState.WaitingForAI:
                if (_aiTask == null || !_aiTask.IsCompleted) { return; }
                ExecuteOneTimeAIChoice();
                break;

            case DecreeState.OpeningDetails:
                // Details page is open — sign it (auto-confirmed by ConfirmSkipPatches).
                SignChosenOneTime();
                break;

            case DecreeState.Signing:
                // Sign confirmed and slot consumed — loop back to ask about the next slot.
                _state = DecreeState.Idle;
                _nextActionTime = Time.time + 0.5f;
                break;

            case DecreeState.Closing:
                ResetState();
                break;
        }
    }

    private unsafe void ReadOneTimeDecreesAndAsk()
    {
        // Read how many slots remain: currentEnactedDecreeCount at +0xB8,
        // MaxNumberOfEnactedDecrees from currentOneTimeDecreesPanelData (+0xD0) → props (+0x38) → int (+0x20).
        int enacted = *(int*)(_oneTimePanel.Pointer + 0xB8);
        int maxEnact = int.MaxValue;
        nint dataPtr = *(nint*)(_oneTimePanel.Pointer + 0xD0);
        if (dataPtr != 0)
        {
            nint propsPtr = *(nint*)(dataPtr + 0x38);
            if (propsPtr != 0) { maxEnact = *(int*)(propsPtr + 0x20); }
        }

        if (enacted >= maxEnact)
        {
            Plugin.Log.LogInfo($"[DecreePanelDriver] OneTime: limit reached ({enacted}/{maxEnact}) — finishing.");
            FinishOneTimePanel();
            return;
        }

        // DecreeListPage at panel+0x58, instantiatedDecrees list at page+0x38
        nint listPagePtr = *(nint*)(_oneTimePanel.Pointer + 0x58);
        if (listPagePtr == 0) { FinishOneTimePanel(); return; }

        nint listPtr = *(nint*)(listPagePtr + 0x38);
        if (listPtr == 0) { FinishOneTimePanel(); return; }

        var templateList = new Il2CppSystem.Collections.Generic.List<TemplateDecree>(listPtr);

        _availableDecrees = new List<DecreeData>();
        var options = new List<(int, string)>();

        for (int i = 0; i < templateList.Count; i++)
        {
            var td = templateList[i];
            if (td == null) { continue; }

            var data = td.GetDecreeData();
            // Skip enacted, flow-disabled, or invalid decrees. !IsValid is the red-X
            // "unavailable" state (TemplateDecree.Setup shows unavailableIcon when
            // !IsEnacted && !IsValid) — the game ignores clicks on these, so selecting
            // one stalls automation.
            if (data == null || data.IsEnacted || !data.IsEnabled || !data.IsValid) { continue; }

            string title = ReadTMP(td.Pointer + 0x20);
            string desc = "";
            nint propsPtr = *(nint*)(data.Pointer + 0x38);
            if (propsPtr != 0)
            {
                nint titleStr = *(nint*)(propsPtr + 0x10);
                nint descStr = *(nint*)(propsPtr + 0x18);
                if (titleStr != 0) { title = ConversationLinePatch.StripTags(new Il2CppSystem.String((IntPtr)titleStr)); }
                if (descStr != 0) { desc = ConversationLinePatch.StripTags(new Il2CppSystem.String((IntPtr)descStr)); }
            }

            int idx = _availableDecrees.Count;
            _availableDecrees.Add(data);
            string label = string.IsNullOrWhiteSpace(desc) ? title : $"{title} — {desc}";
            options.Add((idx, label));
        }

        int slotsLeft = maxEnact - enacted;
        options.Add((_availableDecrees.Count, $"Done — enact no more (used {enacted}/{maxEnact} slots)"));

        Plugin.Log.LogInfo($"[DecreePanelDriver] OneTime: {_availableDecrees.Count} available, {enacted}/{maxEnact} slots used");
        foreach (var (i, t) in options) { Plugin.Log.LogInfo($"  [{i}] {t}"); }

        if (!Plugin.UseAIServer.Value || _availableDecrees.Count == 0)
        {
            FinishOneTimePanel();
            return;
        }

        AIClient.AddContext("One-time decree panel",
            $"{_availableDecrees.Count} decree(s) available, {slotsLeft} slot(s) remaining");
        AIOverlay.ShowThinking();
        var optsCopy = new List<(int, string)>(options);
        _aiTask = Task.Run(() => AIClient.RequestDecision("decree", optsCopy));
        _state = DecreeState.WaitingForAI;
    }

    private void ExecuteOneTimeAIChoice()
    {
        var result = _aiTask.Result;
        _aiTask = null;

        if (result == null)
        {
            Plugin.Log.LogWarning("[DecreePanelDriver] OneTime: AI unreachable — pausing.");
            AIOverlay.ShowError("Cannot reach AI server.");
            Plugin.AutomationEnabled = false;
            Plugin.SafeStopPending = false;
            ResetState();
            return;
        }

        AIOverlay.ShowReasoning(result.Value.reasoning);

        int idx = result.Value.index;
        if (idx >= 0 && idx < _availableDecrees.Count)
        {
            // EnactDecree(name) pops its own "Are you sure?" confirm whose Yes callback depends on
            // state set up inside EnactDecree's body, so ConfirmSkipPatches can't drive it and the
            // popup is left hanging — the decree never enacts. Use the details-page sign flow
            // instead (same machinery the reusable panel uses): open details, then OnSignClick(),
            // which ConfirmSkipPatches auto-confirms (DecreeSign) → panel.OnSignConfirmed enacts.
            _chosenDecree = _availableDecrees[idx];
            Plugin.Log.LogInfo($"[DecreePanelDriver] OneTime: enacting {_chosenDecree.NameInDatabase} — opening details.");
            _oneTimePanel.OnDecreeClick(_chosenDecree);
            _state = DecreeState.OpeningDetails;
            _nextActionTime = Time.time + 0.5f; // let the details page settle
        }
        else
        {
            Plugin.Log.LogInfo("[DecreePanelDriver] OneTime: AI is done enacting decrees.");
            FinishOneTimePanel();
        }
    }

    private unsafe void SignChosenOneTime()
    {
        // Details page lives at OneTimeDecreesPanel + 0x60 (decreeDetailsPage).
        nint detailsPtr = *(nint*)(_oneTimePanel.Pointer + 0x60);
        if (detailsPtr != 0)
        {
            Plugin.Log.LogInfo("[DecreePanelDriver] OneTime: signing via details page.");
            new DecreeDetailsPage((IntPtr)detailsPtr).OnSignClick();
            // ConfirmSkipPatches intercepts OnSignClick and auto-confirms the dialog,
            // invoking DecreeDetailsPage.onSignConfirmed → OneTimeDecreesPanel.OnSignConfirmed.
        }
        else
        {
            Plugin.Log.LogWarning("[DecreePanelDriver] OneTime: details page null — cannot sign.");
        }
        _state = DecreeState.Signing;
        _nextActionTime = Time.time + 0.5f;
    }

    private void FinishOneTimePanel()
    {
        Plugin.Log.LogInfo("[DecreePanelDriver] OneTimeDecreesPanel — finishing.");
        _oneTimePanel.OnFinishButtonClick();
        _state = DecreeState.Closing;
        _nextActionTime = Time.time + 0.5f;
    }

    private void HandleReusablePanel()
    {
        switch (_state)
        {
            case DecreeState.Idle:
                ReadDecreesAndAsk();
                break;

            case DecreeState.WaitingForAI:
                if (_aiTask == null || !_aiTask.IsCompleted) { return; }
                ExecuteAIChoice();
                break;

            case DecreeState.OpeningDetails:
                // Details page is open — now sign
                SignChosen();
                break;

            case DecreeState.Signing:
                // Signing done — close the panel
                Plugin.Log.LogInfo("[DecreePanelDriver] Closing ReusableDecreesPanel.");
                _reusablePanel.OnCloseButtonClick();
                ResetState();
                _nextActionTime = Time.time + 0.5f;
                break;

            case DecreeState.Closing:
                ResetState();
                break;
        }
    }

    private unsafe void ReadDecreesAndAsk()
    {
        nint listPtr = *(nint*)(_reusablePanel.Pointer + 0xE8);
        if (listPtr == 0) { SkipAndClose(); return; }

        var templateList = new Il2CppSystem.Collections.Generic.List<TemplateDecree>(listPtr);
        if (templateList.Count == 0) { SkipAndClose(); return; }

        _availableDecrees = new List<DecreeData>();
        var options = new List<(int, string)>();

        for (int i = 0; i < templateList.Count; i++)
        {
            var td = templateList[i];
            if (td == null) { continue; }

            var data = td.GetDecreeData();
            // Skip enacted, flow-disabled, or invalid decrees (red-X unavailable state).
            if (data == null || data.IsEnacted || !data.IsEnabled || !data.IsValid) { continue; }

            // Read title and description from DecreeProperties at data + 0x38
            string title = ReadTMP(td.Pointer + 0x20); // TMP fallback
            string desc = "";
            nint propsPtr = *(nint*)(data.Pointer + 0x38);
            if (propsPtr != 0)
            {
                nint titleStr = *(nint*)(propsPtr + 0x10);
                nint descStr = *(nint*)(propsPtr + 0x18);
                if (titleStr != 0) { title = ConversationLinePatch.StripTags(new Il2CppSystem.String((IntPtr)titleStr)); }
                if (descStr != 0) { desc = ConversationLinePatch.StripTags(new Il2CppSystem.String((IntPtr)descStr)); }
            }

            int idx = _availableDecrees.Count;
            _availableDecrees.Add(data);
            string label = string.IsNullOrWhiteSpace(desc) ? title : $"{title} — {desc}";
            options.Add((idx, label));
        }

        // Always give the AI the option to sign nothing
        options.Add((_availableDecrees.Count, "Sign no decree — close the panel"));

        Plugin.Log.LogInfo($"[DecreePanelDriver] ReusableDecreesPanel — {_availableDecrees.Count} available");
        foreach (var (i, t) in options) { Plugin.Log.LogInfo($"  [{i}] {t}"); }

        if (!Plugin.UseAIServer.Value || _availableDecrees.Count == 0)
        {
            SkipAndClose();
            return;
        }

        AIClient.AddContext("Decree panel", $"{_availableDecrees.Count} decree(s) available to sign");
        AIOverlay.ShowThinking();
        var optsCopy = new List<(int, string)>(options);
        _aiTask = Task.Run(() => AIClient.RequestDecision("decree", optsCopy));
        _state = DecreeState.WaitingForAI;
    }

    private void ExecuteAIChoice()
    {
        var result = _aiTask.Result;
        _aiTask = null;

        if (result == null)
        {
            Plugin.Log.LogWarning("[DecreePanelDriver] AI server unreachable — pausing.");
            AIOverlay.ShowError("Cannot reach AI server.");
            Plugin.AutomationEnabled = false;
            Plugin.SafeStopPending = false;
            ResetState();
            return;
        }

        AIOverlay.ShowReasoning(result.Value.reasoning);

        int idx = result.Value.index;
        if (idx < 0 || idx >= _availableDecrees.Count)
        {
            Plugin.Log.LogInfo("[DecreePanelDriver] AI chose not to sign any decree.");
            SkipAndClose();
            return;
        }

        _chosenDecree = _availableDecrees[idx];
        Plugin.Log.LogInfo("[DecreePanelDriver] AI chose a decree — opening details page.");
        _reusablePanel.OnDecreeClick(_chosenDecree);
        _state = DecreeState.OpeningDetails;
        _nextActionTime = Time.time + 0.5f; // let details page settle
    }

    private unsafe void SignChosen()
    {
        nint detailsPtr = *(nint*)(_reusablePanel.Pointer + 0xC0);
        if (detailsPtr != 0)
        {
            Plugin.Log.LogInfo("[DecreePanelDriver] Signing decree via details page.");
            new DecreeDetailsPage((IntPtr)detailsPtr).OnSignClick();
            // ConfirmSkipPatches intercepts OnSignClick and auto-confirms the dialog
        }
        _state = DecreeState.Signing;
        _nextActionTime = Time.time + 0.5f;
    }

    private void SkipAndClose()
    {
        Plugin.Log.LogInfo("[DecreePanelDriver] Closing ReusableDecreesPanel without signing.");
        _reusablePanel.OnCloseButtonClick();
        ResetState();
        _nextActionTime = Time.time + 0.5f;
    }

    private void ResetState()
    {
        _state = DecreeState.Idle;
        _aiTask = null;
        _availableDecrees = null;
        _chosenDecree = null;
    }

    private static unsafe string ReadTMP(nint fieldAddress)
    {
        nint objPtr = *(nint*)fieldAddress;
        if (objPtr == 0) { return ""; }
        return ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)objPtr).text ?? "");
    }
}
