using HarmonyLib;
using Il2CppInterop.Runtime;
using Il2CppInterop.Runtime.InteropTypes;
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;

namespace ShadowPresident;

// Auto-confirms Suzerain's in-game "Are you sure?" popups when automation is active.
// Only fires when Plugin.AutomationEnabled is true — game is fully vanilla otherwise.
// System/destructive popups (quit, load, back to menu) are never automated.
internal static class ConfirmSkipPatches
{
    private static readonly Target[] Targets =
    {
        //                declaring type                  method                          confirm lambda                          label
        new Target(typeof(PagedDecisionPanel),            "OnFinish",                     "_OnFinish_b__48_1",                    "Decisions"),
        new Target(typeof(WarProductionPanel),            "OnTrainButtonClick",           "_OnTrainButtonClick_b__93_1",          "WarProduction"),
        new Target(typeof(SkipProloguePanel),             "OnConfirmClick",               "_OnConfirmClick_b__24_0",              "SkipPrologue"),
        new Target(typeof(TemplateArchetypeSlot),         "OnSelectArchetypeButtonClick", "_OnSelectArchetypeButtonClick_b__17_0","Archetype"),
        new Target(typeof(CharacterCustomizationPanel),   "OnFinish",                     "_OnFinish_b__33_0",                    "Customization"),
        new Target(typeof(LoadArchetypePanel),            "OnConfirmSaveFileSelection",   "_OnConfirmSaveFileSelection_b__21_0",  "LoadArchetype"),
        new Target(typeof(DecreeDetailsPage),             "OnSignClick",                  "_OnSignClick_b__0",                    "DecreeSign"),
        new Target(typeof(OneTimeDecreesPanel),           "OnFinishButtonClick",          "_OnFinishButtonClick_b__37_0",         "OneTimeDecrees"),
    };

    private static readonly Dictionary<MethodBase, Target> _byMethod = new();

    public static void ApplyAll(Harmony harmony)
    {
        MethodInfo prefix = AccessTools.Method(typeof(ConfirmSkipPatches), nameof(SkipPrefix));

        foreach (Target t in Targets)
        {
            MethodInfo target = AccessTools.Method(t.Type, t.Method);
            if (target == null)
            {
                Plugin.Log.LogError($"[ConfirmSkip] Target not found: {t.Type.Name}.{t.Method} ({t.Label}) — skipped");
                continue;
            }

            _byMethod[target] = t;
            harmony.Patch(target, prefix: new HarmonyMethod(prefix));
            Plugin.Log.LogInfo($"[ConfirmSkip] Patched {t.Type.Name}.{t.Method} ({t.Label})");
        }
    }

    private static bool SkipPrefix(object __instance, MethodBase __originalMethod)
    {
        if (!Plugin.AutomationEnabled) { return true; }
        if (!_byMethod.TryGetValue(__originalMethod, out Target t)) { return true; }

        // If another mod (e.g. SuzerainUnbound) has also patched this method, defer to it:
        // suppress without invoking so only one invocation of the confirm lambda occurs.
        foreach (var patch in Harmony.GetPatchInfo(__originalMethod).Prefixes)
        {
            if (patch.PatchMethod.DeclaringType?.Namespace == "SuzerainUnbound")
            {
                Plugin.Log.LogInfo($"[ConfirmSkip] Deferring '{t.Label}' to SuzerainUnbound.");
                return false; // cancel original without re-invoking the lambda
            }
        }

        if (TryInvokeConfirm(__instance, t.Type, t.ConfirmLambda, t.Label))
        {
            Plugin.Log.LogInfo($"[ConfirmSkip] Auto-confirmed: {t.Label}");
            return false;
        }

        Plugin.Log.LogWarning($"[ConfirmSkip] Could not invoke confirm for '{t.Label}' — showing popup instead");
        return true;
    }

    private static bool TryInvokeConfirm(object instance, Type panelType, string lambdaName, string label)
    {
        try
        {
            MethodInfo m = FindMethod(panelType, lambdaName);
            if (m != null) { m.Invoke(instance, null); return true; }

            foreach (Type nt in panelType.GetNestedTypes(BindingFlags.Public | BindingFlags.NonPublic))
            {
                MethodInfo nm = FindMethod(nt, lambdaName);
                if (nm == null) { continue; }

                object singleton = GetDisplayClassSingleton(nt);
                object target = singleton ?? CreateInstance(nt);
                if (target == null)
                {
                    Plugin.Log.LogError($"[ConfirmSkip] Found {nt.Name}.{lambdaName} for '{label}' but could not obtain an instance");
                    return false;
                }

                if (singleton == null && instance != null)
                {
                    try
                    {
                        var tBase = target as Il2CppObjectBase;
                        var iBase = instance as Il2CppObjectBase;
                        if (tBase != null && iBase != null)
                        {
                            IntPtr tPtr = IL2CPP.Il2CppObjectBaseToPtr(tBase);
                            IntPtr iPtr = IL2CPP.Il2CppObjectBaseToPtr(iBase);
                            if (tPtr != IntPtr.Zero && iPtr != IntPtr.Zero)
                                Marshal.WriteIntPtr(IntPtr.Add(tPtr, 0x10), iPtr);
                        }
                    }
                    catch { }
                }

                nm.Invoke(target, null);
                return true;
            }

            Plugin.Log.LogError($"[ConfirmSkip] Could not resolve lambda '{lambdaName}' on {panelType.Name} for '{label}'");
            return false;
        }
        catch (Exception e)
        {
            Plugin.Log.LogError($"[ConfirmSkip] Invoking confirm for '{label}' threw: {e}");
            return false;
        }
    }

    private static MethodInfo FindMethod(Type type, string name) =>
        type.GetMethod(name, BindingFlags.Instance | BindingFlags.Static |
                             BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);

    private static object GetDisplayClassSingleton(Type displayClass)
    {
        const BindingFlags F = BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
        foreach (PropertyInfo p in displayClass.GetProperties(F))
        {
            if (p.PropertyType != displayClass || p.GetMethod == null) { continue; }
            try { object v = p.GetValue(null); if (v != null) { return v; } } catch { }
        }
        foreach (FieldInfo f in displayClass.GetFields(F))
        {
            if (f.FieldType != displayClass) { continue; }
            try { object v = f.GetValue(null); if (v != null) { return v; } } catch { }
        }
        return null;
    }

    private static object CreateInstance(Type t)
    {
        try { return Activator.CreateInstance(t); }
        catch (Exception e) { Plugin.Log.LogWarning($"[ConfirmSkip] Could not instantiate {t.Name}: {e.Message}"); return null; }
    }

    private readonly struct Target(Type type, string method, string confirmLambda, string label)
    {
        public readonly Type Type = type;
        public readonly string Method = method;
        public readonly string ConfirmLambda = confirmLambda;
        public readonly string Label = label;
    }
}
