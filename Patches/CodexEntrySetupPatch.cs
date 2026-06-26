using System.Threading.Tasks;
using HarmonyLib;

namespace ShadowPresident;

// Intercept GoToCodexEntry before the panel opens, read the data, cancel the UI.
// This lets CodexReader call GoToCodexEntryByArticyId freely without any panel
// ever appearing on screen.
[HarmonyPatch(typeof(CodexPanel), nameof(CodexPanel.GoToCodexEntry))]
public class CodexEntrySetupPatch
{
    static bool Prefix(CodexEntryData codexEntryData)
    {
        if (codexEntryData == null) { return true; }

        string articyId = codexEntryData.Id           ?? "";
        string nameInDb = codexEntryData.NameInDatabase ?? "";
        if (string.IsNullOrWhiteSpace(articyId)) { return true; }

        string title = "";
        string desc  = "";
        unsafe
        {
            nint propsPtr = *(nint*)(codexEntryData.Pointer + 0x38);
            if (propsPtr != 0)
            {
                nint titlePtr = *(nint*)(propsPtr + 0x28);
                nint descPtr  = *(nint*)(propsPtr + 0x30);
                if (titlePtr != 0) { title = new Il2CppSystem.String((System.IntPtr)titlePtr); }
                if (descPtr  != 0) { desc  = new Il2CppSystem.String((System.IntPtr)descPtr); }
            }
        }

        if (string.IsNullOrWhiteSpace(title)) { return true; }

        Plugin.Log.LogInfo($"[Codex] Captured: {title}");
        var snapshot = (articyId, nameInDb, title, desc);
        Task.Run(() => CodexReader.PostEntry(snapshot.articyId, snapshot.nameInDb,
                                              snapshot.title,   snapshot.desc));

        // Return false = cancel the original method so the panel never opens.
        return false;
    }
}
