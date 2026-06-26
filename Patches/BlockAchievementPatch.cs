using HarmonyLib;
using PixelCrushers.DialogueSystem.SequencerCommands;

namespace ShadowPresident;

[HarmonyPatch(typeof(SequencerCommandUnlockSteamAchievement), nameof(SequencerCommandUnlockSteamAchievement.Start))]
public class BlockAchievementPatch
{
    static bool Prefix(SequencerCommandUnlockSteamAchievement __instance)
    {
        if (!Plugin.BlockAchievements.Value) { return true; }

        string achievementId = "(unknown)";
        try
        {
            string[] parameters = Traverse.Create(__instance).Property("parameters").GetValue<string[]>();
            if (parameters != null && parameters.Length > 0) { achievementId = parameters[0]; }
        }
        catch { }

        Plugin.Log.LogInfo($"[BlockAchievements] Suppressed: {achievementId}");
        return false;
    }
}
