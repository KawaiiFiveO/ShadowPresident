using BepInEx;
using BepInEx.Configuration;
using BepInEx.Logging;
using BepInEx.Unity.IL2CPP;
using HarmonyLib;
using Il2CppInterop.Runtime.Injection;
using UnityEngine;

namespace ShadowPresident;

[BepInPlugin(MyPluginInfo.PLUGIN_GUID, MyPluginInfo.PLUGIN_NAME, MyPluginInfo.PLUGIN_VERSION)]
public class Plugin : BasePlugin
{
    internal static new ManualLogSource Log;

    public static bool AutomationEnabled;
    public static bool SafeStopPending;
    public static ConfigEntry<bool> AutomationEnabledOnLoad;
    public static ConfigEntry<CustomKey> ToggleKey;
    public static ConfigEntry<CustomKey> SafeStopKey;
    public static ConfigEntry<CustomKey> MinimizeKey;
    public static ConfigEntry<int> BackgroundFps;
    public static ConfigEntry<bool> BlockAchievements;
    public static ConfigEntry<bool> UseAIServer;
    public static ConfigEntry<string> AIServerUrl;
    public static ConfigEntry<int> AIContextLines;
    public static ConfigEntry<float> OverlayDuration;
    public override void Load()
    {
        Log = base.Log;

        Log.LogMessage("==========================================================================");
        Log.LogMessage($"  Shadow President v{MyPluginInfo.PLUGIN_VERSION} loaded!");
        Log.LogMessage("==========================================================================");

        AutomationEnabledOnLoad = Config.Bind("Automation", "EnabledOnLoad", false,
            "Whether automation starts active when the plugin loads.");
        ToggleKey = Config.Bind("Automation", "ToggleKey", CustomKey.F6,
            "Key to toggle automation on/off at runtime.");
        SafeStopKey = Config.Bind("Automation", "SafeStopKey", CustomKey.F9,
            "Key to stop automation cleanly at the next checkpoint boundary, ensuring the server log and game save are in sync before quitting.");
        MinimizeKey = Config.Bind("Automation", "MinimizeKey", CustomKey.F8,
            "Key to minimize the window to the taskbar while automation runs in background.");
        BackgroundFps = Config.Bind("Automation", "BackgroundFps", 30,
            "Frame rate cap while the window is minimized. 30 is the game's minimum supported rate; "
            + "going lower starves PCDS conversation-transition coroutines and stalls dialogue. "
            + "The game's normal FPS setting is restored on focus.");

        BlockAchievements = Config.Bind("Automation", "BlockAchievements", true,
            "Suppress Steam achievement unlocks entirely.");

        UseAIServer = Config.Bind("AI", "UseAIServer", true,
            "Send decisions to the AI server. If false, all choices are made randomly.");
        AIServerUrl = Config.Bind("AI", "ServerUrl", "http://localhost:1954",
            "URL of the Shadow President AI server.");
        AIContextLines = Config.Bind("AI", "ContextLines", 500,
            "Rolling dialogue buffer size sent to the server. The server trims to its max_context_tokens budget before sending to the LLM.");
        OverlayDuration = Config.Bind("AI", "OverlayDuration", 30f,
            "Seconds the AI reasoning overlay stays visible before fading. Resets on each new reasoning.");

        AutomationEnabled = AutomationEnabledOnLoad.Value;
        Log.LogInfo($"[Automation] Starting {(AutomationEnabled ? "ENABLED" : "DISABLED")}. Press {ToggleKey.Value} to toggle, {SafeStopKey.Value} to stop at next checkpoint.");

        var harmony = new Harmony(MyPluginInfo.PLUGIN_GUID);
        ConfirmSkipPatches.ApplyAll(harmony);
        Log.LogInfo("[ConfirmSkip] Patches applied.");


        harmony.PatchAll(typeof(Plugin).Assembly);
        Log.LogInfo("[Harmony] All assembly patches applied.");

        ClassInjector.RegisterTypeInIl2Cpp<AIOverlay>();
        var overlayObj = new GameObject("ShadowPresidentAIOverlay");
        overlayObj.AddComponent<AIOverlay>();
        GameObject.DontDestroyOnLoad(overlayObj);
        Log.LogInfo("[AIOverlay] Injected.");

        ClassInjector.RegisterTypeInIl2Cpp<AutomationController>();
        var controllerObj = new GameObject("ShadowPresidentAutomationController");
        controllerObj.AddComponent<AutomationController>();
        GameObject.DontDestroyOnLoad(controllerObj);
        Log.LogInfo("[Automation] AutomationController injected.");

        ClassInjector.RegisterTypeInIl2Cpp<DialogueDriver>();
        var driverObj = new GameObject("ShadowPresidentDialogueDriver");
        driverObj.AddComponent<DialogueDriver>();
        GameObject.DontDestroyOnLoad(driverObj);
        Log.LogInfo("[DialogueDriver] DialogueDriver injected.");

        ClassInjector.RegisterTypeInIl2Cpp<DecisionDriver>();
        var decisionObj = new GameObject("ShadowPresidentDecisionDriver");
        decisionObj.AddComponent<DecisionDriver>();
        GameObject.DontDestroyOnLoad(decisionObj);
        Log.LogInfo("[DecisionDriver] DecisionDriver injected.");

        ClassInjector.RegisterTypeInIl2Cpp<BillDriver>();
        var billObj = new GameObject("ShadowPresidentBillDriver");
        billObj.AddComponent<BillDriver>();
        GameObject.DontDestroyOnLoad(billObj);
        Log.LogInfo("[BillDriver] BillDriver injected.");

        ClassInjector.RegisterTypeInIl2Cpp<PagedDecisionDriver>();
        var pagedObj = new GameObject("ShadowPresidentPagedDecisionDriver");
        pagedObj.AddComponent<PagedDecisionDriver>();
        GameObject.DontDestroyOnLoad(pagedObj);
        Log.LogInfo("[PagedDecisionDriver] PagedDecisionDriver injected.");

        ClassInjector.RegisterTypeInIl2Cpp<DecreePanelDriver>();
        var decreeObj = new GameObject("ShadowPresidentDecreePanelDriver");
        decreeObj.AddComponent<DecreePanelDriver>();
        GameObject.DontDestroyOnLoad(decreeObj);
        Log.LogInfo("[DecreePanelDriver] DecreePanelDriver injected.");

        ClassInjector.RegisterTypeInIl2Cpp<PrologueEpilogueDriver>();
        var prologueObj = new GameObject("ShadowPresidentPrologueEpilogueDriver");
        prologueObj.AddComponent<PrologueEpilogueDriver>();
        GameObject.DontDestroyOnLoad(prologueObj);
        Log.LogInfo("[PrologueEpilogueDriver] PrologueEpilogueDriver injected.");

        ClassInjector.RegisterTypeInIl2Cpp<CodexReader>();
        var codexObj = new GameObject("ShadowPresidentCodexReader");
        codexObj.AddComponent<CodexReader>();
        GameObject.DontDestroyOnLoad(codexObj);
        Log.LogInfo("[CodexReader] Injected.");

        ClassInjector.RegisterTypeInIl2Cpp<GameStateReader>();
        var gsrObj = new GameObject("ShadowPresidentGameStateReader");
        gsrObj.AddComponent<GameStateReader>();
        GameObject.DontDestroyOnLoad(gsrObj);
        Log.LogInfo("[GameStateReader] Injected.");

        ClassInjector.RegisterTypeInIl2Cpp<BackgroundController>();
        var bgObj = new GameObject("ShadowPresidentBackgroundController");
        bgObj.AddComponent<BackgroundController>();
        GameObject.DontDestroyOnLoad(bgObj);
        Log.LogInfo("[BackgroundController] Injected.");

        ClassInjector.RegisterTypeInIl2Cpp<CharacterCustomizationDriver>();
        var customizationObj = new GameObject("ShadowPresidentCharacterCustomizationDriver");
        customizationObj.AddComponent<CharacterCustomizationDriver>();
        GameObject.DontDestroyOnLoad(customizationObj);
        Log.LogInfo("[CharacterCustomizationDriver] CharacterCustomizationDriver injected.");
    }
}
