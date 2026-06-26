using HarmonyLib;

namespace ShadowPresident;

[HarmonyPatch(typeof(PagedDecisionPanel), nameof(PagedDecisionPanel.Show))]
public class PagedDecisionPanelShowPatch
{
    internal static bool IsShowing;

    static void Postfix() => IsShowing = true;
}

[HarmonyPatch(typeof(PagedDecisionPanel), nameof(PagedDecisionPanel.Hide))]
public class PagedDecisionPanelHidePatch
{
    static void Postfix() => PagedDecisionPanelShowPatch.IsShowing = false;
}
