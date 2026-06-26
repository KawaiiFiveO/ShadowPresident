using HarmonyLib;
using System.Threading.Tasks;

namespace ShadowPresident;

[HarmonyPatch(typeof(GameFlowManager), nameof(GameFlowManager.FinishStoryFragment))]
public class CheckpointPatch
{
    static void Postfix(GameFlowManager __instance, StoryFragmentData storyFragment)
    {
        AIClient.CurrentTurn = __instance.CurrentTurnNo;
        AIClient.CurrentStep = __instance.CurrentStepNo;

        // Log which fragment finished so we can trace duplicate-trigger issues.
        if (storyFragment != null)
        {
            unsafe
            {
                nint namePtr = *(nint*)(storyFragment.Pointer + 0x18);
                if (namePtr != 0)
                    Plugin.Log.LogInfo($"[Checkpoint] FinishStoryFragment: {new Il2CppSystem.String((System.IntPtr)namePtr)}");
            }
        }

        // Read NameInDatabase from StoryFragmentData — present at 0x18 on all subtypes
        if (storyFragment != null)
        {
            unsafe
            {
                nint namePtr = *(nint*)(storyFragment.Pointer + 0x18);
                if (namePtr != 0)
                    AIClient.CurrentFragment = new Il2CppSystem.String((System.IntPtr)namePtr);
            }
        }

        if (Plugin.SafeStopPending)
        {
            Plugin.SafeStopPending = false;
            Plugin.AutomationEnabled = false;
            AIOverlay.ShowReasoning("Stopped at checkpoint — safe to quit.");
            Plugin.Log.LogInfo("[Automation] Safe-stop: disabled at checkpoint boundary.");
        }

        // Dismiss reports that were active during this fragment — runs on main thread before
        // the async checkpoint post so Il2Cpp list access is safe.
        GameStateReader.DismissBufferedReports(__instance);
        // Clear stale reports from C# cache and schedule a read 1.5s from now so the game
        // has time to add new reports before we pick them up for the next fragment.
        AIClient.CurrentReports = "";
        GameStateReader.ScheduleEarlyRead(1.5f);

        Task.Run(() => AIClient.PostCheckpoint());
    }
}
