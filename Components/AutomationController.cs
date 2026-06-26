using System;
using UnityEngine;
using UnityEngine.InputSystem;

namespace ShadowPresident;

public class AutomationController : MonoBehaviour
{
    public AutomationController(IntPtr ptr) : base(ptr) { }

    void Update()
    {
        if (Keyboard.current == null) { return; }

        if (Keyboard.current[InputHelper.ToInputKey(Plugin.ToggleKey.Value)].wasPressedThisFrame)
        {
            Plugin.AutomationEnabled = !Plugin.AutomationEnabled;
            if (!Plugin.AutomationEnabled)
            {
                Plugin.SafeStopPending = false;
                AIOverlay.Hide();
            }
            Plugin.Log.LogInfo($"[Automation] {(Plugin.AutomationEnabled ? "ENABLED" : "DISABLED")}");
        }

        if (Keyboard.current[InputHelper.ToInputKey(Plugin.SafeStopKey.Value)].wasPressedThisFrame
            && Plugin.AutomationEnabled)
        {
            Plugin.SafeStopPending = !Plugin.SafeStopPending;
            if (Plugin.SafeStopPending)
            {
                Plugin.Log.LogInfo("[Automation] Safe-stop requested — will stop at next checkpoint.");
                AIOverlay.ShowPending("Stopping at next checkpoint…");
            }
            else
            {
                Plugin.Log.LogInfo("[Automation] Safe-stop cancelled.");
                AIOverlay.Hide();
            }
        }
    }
}
