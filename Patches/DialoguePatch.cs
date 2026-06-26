using HarmonyLib;
using Il2CppInterop.Runtime.InteropTypes.Arrays;
using PixelCrushers.DialogueSystem;
using System.Text.RegularExpressions;

namespace ShadowPresident;

[HarmonyPatch(typeof(ConversationHandler), nameof(ConversationHandler.OnConversationLine))]
public class ConversationLinePatch
{
    private static string _lastText = string.Empty;

    // Matches TMP link tags: <link="codex_entry_id">
    private static readonly Regex _linkPattern = new(@"<link=""([^""]+)"">");

    static void Postfix(Subtitle subtitle)
    {
        if (subtitle?.speakerInfo == null || subtitle.formattedText == null) { return; }
        string raw  = subtitle.formattedText.text;
        string text = StripTags(raw);
        if (string.IsNullOrEmpty(text) || text == _lastText) { return; }
        _lastText = text;
        string speaker = subtitle.speakerInfo.Name ?? subtitle.speakerInfo.nameInDatabase;
        Plugin.Log.LogInfo($"[Dialogue] {speaker}: {text}");
        AIClient.AddContext(speaker, text);
        ExtractLinks(raw);
    }

    internal static void ExtractLinks(string raw)
    {
        foreach (System.Text.RegularExpressions.Match m in _linkPattern.Matches(raw))
        {
            string id = m.Groups[1].Value;
            CodexReader.QueueLookup(id);  // queue for auto-open
            AIClient.AddCodexRef(id);     // track for injection into next decision
        }
    }

    internal static string StripTags(string text)
    {
        text = Regex.Replace(text, @"\{[^}]*\}", "");
        text = Regex.Replace(text, @"<[^>]*>", "");
        return text.Trim();
    }
}


[HarmonyPatch(typeof(ConversationHandler), nameof(ConversationHandler.OnConversationResponseMenu))]
public class ConversationResponseMenuPatch
{
    private static string _lastKey = string.Empty;

    static void Postfix(Il2CppReferenceArray<Response> responses)
    {
        if (responses == null || responses.Length == 0) { return; }

        var texts = new System.Collections.Generic.List<string>(responses.Length);
        for (int i = 0; i < responses.Length; i++)
        {
            Response response = responses[i];
            if (response == null) { continue; }
            texts.Add(ConversationLinePatch.StripTags(response.formattedText?.text ?? ""));
        }

        string key = string.Join("|", texts);
        if (key == _lastKey) { return; }
        _lastKey = key;

        Plugin.Log.LogInfo("[Dialogue] Choices:");
        for (int i = 0; i < texts.Count; i++)
        {
            Response response = responses[i];
            string disabled = (response != null && !response.enabled) ? " (disabled)" : "";
            Plugin.Log.LogInfo($"[Dialogue]   [{i}]{disabled} {texts[i]}");
        }
    }
}
