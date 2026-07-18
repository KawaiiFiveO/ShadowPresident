using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using UnityEngine;

namespace ShadowPresident;

public class BillDriver : MonoBehaviour
{
    public BillDriver(IntPtr ptr) : base(ptr) { }

    private BillPanel _billPanel;
    private Task<(int index, string reasoning)?> _aiTask;
    private bool _handled;   // true once we've acted on the current panel showing
    private string _pendingBillTitle = "";

    void Update()
    {
        if (!Plugin.AutomationEnabled) { return; }

        if (_billPanel == null) { _billPanel = FindObjectOfType<BillPanel>(); }
        // Panel not up — clear state so the next bill (a fresh showing) is handled. Dropping a
        // still-running task here is fine: the bill is gone, so there is nothing to act on.
        if (_billPanel == null || !_billPanel.IsShowing()) { _handled = false; _aiTask = null; return; }

        // Poll a running AI decision.
        if (_aiTask != null)
        {
            if (!_aiTask.IsCompleted) { return; }
            ExecuteAIChoice();
            return;
        }

        // Already acted on this showing — wait for the panel to close before touching it again.
        // SignBill()/VetoBill() take a frame (plus an auto-confirmed dialog) to dismiss the panel;
        // without this guard the driver starts a SECOND decision for the same bill while it is
        // still on screen, producing an out-of-order double action.
        if (_handled) { return; }
        _handled = true;

        if (!Plugin.UseAIServer.Value)
        {
            bool sign = UnityEngine.Random.value > 0.5f;
            Plugin.Log.LogInfo($"[BillDriver] {(sign ? "Signing" : "Vetoing")} bill (random).");
            if (sign) { _billPanel.SignBill(); } else { _billPanel.VetoBill(); }
            return;
        }

        // Extract bill text on main thread before starting task
        _pendingBillTitle = ReadTMPText(_billPanel.Pointer + 0x28);
        string desc = ReadTMPText(_billPanel.Pointer + 0x30);
        AIClient.AddContext("Bill for decision", $"{_pendingBillTitle} — {desc}");

        var choices = new List<(int, string)> { (0, $"Sign: {_pendingBillTitle}"), (1, $"Veto: {_pendingBillTitle}") };
        GameState.EnsureRead();
        AIOverlay.ShowThinking();
        _aiTask = Task.Run(() => AIClient.RequestDecision("bill", choices));
    }

    private void ExecuteAIChoice()
    {
        var result = _aiTask.Result;
        _aiTask = null;
        // Keep _handled = true: it is cleared only when the panel closes, so we never start a
        // second decision while Sign/Veto is still dismissing this bill.

        if (result == null)
        {
            Plugin.Log.LogWarning("[BillDriver] AI server unreachable — pausing automation.");
            AIOverlay.ShowError("Cannot reach AI server.");
            Plugin.AutomationEnabled = false;
            Plugin.SafeStopPending = false;
            return;
        }

        bool sign = result.Value.index == 0;
        Plugin.Log.LogInfo($"[BillDriver] {(sign ? "Signing" : "Vetoing")} bill.");
        AIOverlay.ShowReasoning(result.Value.reasoning);
        AIClient.AddContext("[CHOICE]", sign ? $"Sign: {_pendingBillTitle}" : $"Veto: {_pendingBillTitle}");
        if (sign) { _billPanel.SignBill(); } else { _billPanel.VetoBill(); }
    }

    private static string ReadTMPText(nint componentOffset)
    {
        unsafe
        {
            nint ptr = *(nint*)componentOffset;
            if (ptr == 0) { return ""; }
            return ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)ptr).text ?? "");
        }
    }
}
