using HarmonyLib;
using UnityEngine;

namespace ShadowPresident;

[HarmonyPatch(typeof(PersistenceManager), "Awake")]
public class BackgroundExecutionPatch
{
    static void Postfix()
    {
        Application.runInBackground = true;
        Plugin.Log.LogInfo("[RunInBackground] Application.runInBackground = true");
    }
}

[HarmonyPatch(typeof(Application), nameof(Application.isFocused), MethodType.Getter)]
public class AlwaysFocusedPatch
{
    static void Postfix(ref bool __result)
    {
        __result = true;
    }
}

// Prevent anything from resetting runInBackground to false — if it gets reset,
// Unity's game loop suspends on minimize and no Update() calls fire.
[HarmonyPatch(typeof(Application), nameof(Application.runInBackground), MethodType.Setter)]
public class RunInBackgroundPatch
{
    static bool Prefix(ref bool value)
    {
        value = true;
        return true;
    }
}
