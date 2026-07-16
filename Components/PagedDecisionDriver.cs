using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using UnityEngine;

namespace ShadowPresident;

public class PagedDecisionDriver : MonoBehaviour
{
    public PagedDecisionDriver(IntPtr ptr) : base(ptr) { }

    private PagedDecisionPanel _panel;

    private int _processedPage = -1;   // page index we have already acted on
    private bool _readyToAdvance = false;
    private float _advanceAt = 0f;
    private Task<(int index, string reasoning)?> _aiTask;

    // Carousel pending state (populated on main thread before task starts)
    private TemplateCarouselChoiceOption _pendingCarousel;
    private int _pendingCarouselCurrentIdx;
    private int _pendingCarouselTotal;

    // Multi-choice pending state
    private List<TemplateMultipleChoiceOption> _pendingMultiOptions;
    private bool _pendingMultiIsRadio;   // single-choice page (max == 1) — selecting must deselect the default
    private Task<(List<int> indices, string reasoning)?> _aiMultiTask;   // checkbox page (max > 1)

    // Option labels for [CHOICE] context line
    private List<(int, string)> _pendingOptions;

    // The question this page asks. Sent with the /decision request and only written to the rolling
    // context afterwards, paired with the answer. Turn 1 opens several paged panels back to back,
    // so the context tail already holds this panel's earlier pages; a question pushed in *before*
    // the request dangles there unanswered, indistinguishable from the settled ones, and the model
    // answers the whole apparent form at once instead of the single page in front of it.
    private string _pendingQuestion = "";

    void Update()
    {
        if (!Plugin.AutomationEnabled) { return; }

        if (_panel == null) { _panel = FindObjectOfType<PagedDecisionPanel>(); }
        if (_panel == null) { return; }

        unsafe { if (!*(bool*)(_panel.Pointer + 0x120)) { ResetState(); return; } }

        // Wait out post-selection pause before advancing page
        if (_readyToAdvance)
        {
            if (Time.time < _advanceAt) { return; }
            _readyToAdvance = false;
            AdvancePage();
            return;
        }

        // Poll running AI task
        if (_aiTask != null)
        {
            if (!_aiTask.IsCompleted) { return; }
            ExecuteAIChoice();
            return;
        }

        // Poll running multi-select AI task (checkbox page)
        if (_aiMultiTask != null)
        {
            if (!_aiMultiTask.IsCompleted) { return; }
            ExecuteAIMultiChoice();
            return;
        }

        int currentPage;
        unsafe { currentPage = *(int*)(_panel.Pointer + 0xE4); }
        if (currentPage == _processedPage) { return; }

        ProcessPage(currentPage);
    }

    private void ProcessPage(int pageIndex)
    {
        _processedPage = pageIndex;

        unsafe
        {
            nint listPtr = *(nint*)(_panel.Pointer + 0xA0);
            if (listPtr == 0) { SetAdvanceTimer(); return; }

            var pages = new Il2CppSystem.Collections.Generic.List<TemplatePagedDecisionsPage>(listPtr);
            if (pageIndex >= pages.Count) { SetAdvanceTimer(); return; }

            var page = pages[pageIndex];
            if (page == null) { SetAdvanceTimer(); return; }

            string pageTitle = ReadTMP(page.Pointer + 0x40);
            string pageDesc = ReadTMP(page.Pointer + 0x48);

            // Some pages carry no per-page title/description — notably the Turn-1 term-focus
            // carousel, whose options are self-describing policy areas. Left blank, the model is
            // asked to choose with no question at all and hallucinates what it's deciding. Fall
            // back to the panel's own title/description (panel +0x28/+0x30) —
            // PagedDecisionPanelProperties.Title is a RequiredField, so this is always populated.
            if (string.IsNullOrWhiteSpace(pageTitle))
            {
                pageTitle = ReadTMP(_panel.Pointer + 0x28);
                if (string.IsNullOrWhiteSpace(pageDesc)) { pageDesc = ReadTMP(_panel.Pointer + 0x30); }
            }

            nint carouselDataPtr = *(nint*)(page.Pointer + 0x90);
            nint multiDataPtr = *(nint*)(page.Pointer + 0x98);

            if (carouselDataPtr != 0)
                ProcessCarouselPage(page, pageTitle, pageDesc);
            else if (multiDataPtr != 0)
                ProcessMultiPage(page, pageTitle, pageDesc);
            else
            {
                Plugin.Log.LogWarning($"[PagedDecisionDriver] Unknown page type on page {pageIndex}, accepting default.");
                SetAdvanceTimer();
            }
        }
    }

    private unsafe void ProcessCarouselPage(TemplatePagedDecisionsPage page, string pageTitle, string pageDesc)
    {
        nint carouselPtr = *(nint*)(page.Pointer + 0x50);
        if (carouselPtr == 0) { SetAdvanceTimer(); return; }

        var carousel = new TemplateCarouselChoiceOption((IntPtr)carouselPtr);
        int currentIdx = *(int*)(carouselPtr + 0x40);

        nint choiceListPtr = *(nint*)(carouselPtr + 0x58);
        if (choiceListPtr == 0) { SetAdvanceTimer(); return; }

        var choiceList = new Il2CppSystem.Collections.Generic.List<CarouselChoiceOptionData>(choiceListPtr);
        int total = choiceList.Count;
        if (total == 0) { SetAdvanceTimer(); return; }

        // CarouselChoiceOptionProperties: Title at +0x10, Description at +0x18
        var titles = new List<string>(total);
        var descs = new List<string>(total);
        for (int i = 0; i < total; i++)
        {
            var item = choiceList[i];
            string title = "", desc = "";
            if (item != null)
            {
                nint propsPtr = *(nint*)(item.Pointer + 0x38);
                if (propsPtr != 0)
                {
                    nint titlePtr = *(nint*)(propsPtr + 0x10);
                    if (titlePtr != 0) { title = ConversationLinePatch.StripTags(new Il2CppSystem.String((IntPtr)titlePtr)); }

                    nint descPtr = *(nint*)(propsPtr + 0x18);
                    if (descPtr != 0) { desc = ConversationLinePatch.StripTags(new Il2CppSystem.String((IntPtr)descPtr)); }
                }
            }
            titles.Add(title);
            descs.Add(desc);
        }

        // Funding carousels give every option (Maintain/Increase/Decrease) the same Description: it
        // states the department's situation, not what the option does. Appended to each title it
        // triples the prompt and renders three choices that read as identical. Hoist it into the
        // question and leave the options as the bare verbs that actually distinguish them.
        string sharedDesc = SharedDescription(descs);
        if (sharedDesc != null && !pageDesc.Contains(sharedDesc))
        {
            pageDesc = string.IsNullOrWhiteSpace(pageDesc) ? sharedDesc : $"{pageDesc} {sharedDesc}";
        }

        var options = new List<(int, string)>(total);
        for (int i = 0; i < total; i++)
        {
            string label = titles[i];
            if (sharedDesc == null && !string.IsNullOrWhiteSpace(descs[i])) { label = $"{label} — {descs[i]}"; }
            options.Add((i, label));
        }

        _pendingCarousel = carousel;
        _pendingCarouselCurrentIdx = currentIdx;
        _pendingCarouselTotal = total;
        _pendingMultiOptions = null;

        Plugin.Log.LogInfo($"[PagedDecisionDriver] Carousel: \"{pageTitle}\" — {total} options, current={currentIdx}");
        foreach (var (i, t) in options) { Plugin.Log.LogInfo($"  [{i}] {t}"); }

        if (!Plugin.UseAIServer.Value || total == 1)
        {
            int target = total == 1 ? currentIdx : UnityEngine.Random.Range(0, total);
            SelectCarousel(target);
            SetAdvanceTimer();
            return;
        }

        _pendingQuestion = string.IsNullOrWhiteSpace(pageDesc) ? pageTitle : $"{pageTitle}: {pageDesc}";
        AIOverlay.ShowThinking();
        _pendingOptions = new List<(int, string)>(options);
        var question = _pendingQuestion;
        _aiTask = Task.Run(() => AIClient.RequestDecision("paged_decision", _pendingOptions, question));
    }

    private unsafe void ProcessMultiPage(TemplatePagedDecisionsPage page, string pageTitle, string pageDesc)
    {
        nint listPtr = *(nint*)(page.Pointer + 0x78);
        if (listPtr == 0) { SetAdvanceTimer(); return; }

        var il2List = new Il2CppSystem.Collections.Generic.List<TemplateMultipleChoiceOption>(listPtr);
        int count = il2List.Count;
        if (count == 0) { SetAdvanceTimer(); return; }

        // Capture Il2Cpp refs and titles on the main thread
        var optionRefs = new List<TemplateMultipleChoiceOption>(count);
        var options = new List<(int, string)>(count);
        for (int i = 0; i < count; i++)
        {
            var opt = il2List[i];
            optionRefs.Add(opt);
            string title = opt != null ? ReadTMP(opt.Pointer + 0x20) : "";
            options.Add((i, title));
        }

        // Radio (single-choice) vs checkbox: MultipleChoicePageProperties at
        // page.currentMultipleChoicePageData (+0x98) → properties (+0x38). MaximumChoiceCount at
        // +0x24 (max <= 1 ⇒ radio), MinimumChoiceCount at +0x20.
        bool isRadio = true;
        int minChoices = 1, maxChoices = 1;
        nint pageDataPtr = *(nint*)(page.Pointer + 0x98);
        if (pageDataPtr != 0)
        {
            nint propsPtr = *(nint*)(pageDataPtr + 0x38);
            if (propsPtr != 0)
            {
                minChoices = *(int*)(propsPtr + 0x20);
                maxChoices = *(int*)(propsPtr + 0x24);
                isRadio = maxChoices <= 1;
            }
        }

        _pendingMultiOptions = optionRefs;
        _pendingMultiIsRadio = isRadio;
        _pendingCarousel = null;

        bool multiSelect = !isRadio && maxChoices > 1;
        string kind = isRadio ? "radio" : (multiSelect ? $"checkbox, select {minChoices}-{maxChoices}" : "checkbox");
        Plugin.Log.LogInfo($"[PagedDecisionDriver] MultiChoice: \"{pageTitle}\" — {count} options ({kind})");
        foreach (var (i, t) in options) { Plugin.Log.LogInfo($"  [{i}] {t}"); }

        if (!Plugin.UseAIServer.Value || count == 1)
        {
            int target = count == 1 ? 0 : UnityEngine.Random.Range(0, count);
            SelectMultiOption(target);
            SetAdvanceTimer();
            return;
        }

        _pendingQuestion = string.IsNullOrWhiteSpace(pageDesc) ? pageTitle : $"{pageTitle}: {pageDesc}";
        AIOverlay.ShowThinking();
        _pendingOptions = new List<(int, string)>(options);
        var question = _pendingQuestion;

        if (multiSelect)
        {
            // Checkbox page (e.g. emergency decrees): the AI picks a set within [min, max].
            int min = Math.Max(0, minChoices);
            int max = Math.Min(maxChoices, count);
            _aiMultiTask = Task.Run(() => AIClient.RequestMultiDecision("paged_decision", _pendingOptions, min, max, question));
        }
        else
        {
            _aiTask = Task.Run(() => AIClient.RequestDecision("paged_decision", _pendingOptions, question));
        }
    }

    private void ExecuteAIChoice()
    {
        var result = _aiTask.Result;
        _aiTask = null;

        if (result == null)
        {
            Plugin.Log.LogWarning("[PagedDecisionDriver] AI server unreachable — pausing.");
            AIOverlay.ShowError("Cannot reach AI server.");
            Plugin.AutomationEnabled = false;
            Plugin.SafeStopPending = false;
            return;
        }

        AIOverlay.ShowReasoning(result.Value.reasoning);

        if (_pendingOptions != null && result.Value.index >= 0 && result.Value.index < _pendingOptions.Count)
        {
            RecordSettled(_pendingOptions[result.Value.index].Item2);
        }

        if (_pendingCarousel != null)
        {
            int target = Math.Max(0, Math.Min(result.Value.index, _pendingCarouselTotal - 1));
            SelectCarousel(target);
        }
        else if (_pendingMultiOptions != null)
        {
            int target = Math.Max(0, Math.Min(result.Value.index, _pendingMultiOptions.Count - 1));
            SelectMultiOption(target);
        }

        SetAdvanceTimer();
    }

    private void ExecuteAIMultiChoice()
    {
        var result = _aiMultiTask.Result;
        _aiMultiTask = null;

        if (result == null)
        {
            Plugin.Log.LogWarning("[PagedDecisionDriver] AI server unreachable — pausing.");
            AIOverlay.ShowError("Cannot reach AI server.");
            Plugin.AutomationEnabled = false;
            Plugin.SafeStopPending = false;
            return;
        }

        AIOverlay.ShowReasoning(result.Value.reasoning);

        var indices = result.Value.indices ?? new List<int>();
        if (_pendingOptions != null && indices.Count > 0)
        {
            var labels = new List<string>(indices.Count);
            foreach (int i in indices)
            {
                if (i >= 0 && i < _pendingOptions.Count) { labels.Add(_pendingOptions[i].Item2); }
            }
            if (labels.Count > 0) { RecordSettled(string.Join("; ", labels)); }
        }

        if (_pendingMultiOptions != null)
        {
            Plugin.Log.LogInfo($"[PagedDecisionDriver] MultiChoice → {indices.Count} option(s): [{string.Join(", ", indices)}].");
            for (int i = 0; i < _pendingMultiOptions.Count; i++)
            {
                if (_pendingMultiOptions[i] != null) { _pendingMultiOptions[i].OnValueChanged(false); }
            }
            foreach (int t in indices)
            {
                if (t >= 0 && t < _pendingMultiOptions.Count && _pendingMultiOptions[t] != null)
                {
                    _pendingMultiOptions[t].OnValueChanged(true);
                }
            }
        }

        SetAdvanceTimer();
    }

    // Append the page's question and its answer to the rolling context as one settled pair, once
    // the AI has committed. Both land together so the context can never hold an open question —
    // see _pendingQuestion.
    private void RecordSettled(string answer)
    {
        if (!string.IsNullOrWhiteSpace(_pendingQuestion))
        {
            AIClient.AddContext("Policy decision", _pendingQuestion);
        }
        AIClient.AddContext("[CHOICE]", answer);
        _pendingQuestion = "";
    }

    private void SelectCarousel(int target)
    {
        if (_pendingCarousel == null) { return; }
        int steps = ((target - _pendingCarouselCurrentIdx) % _pendingCarouselTotal + _pendingCarouselTotal) % _pendingCarouselTotal;
        Plugin.Log.LogInfo($"[PagedDecisionDriver] Carousel → option {target} ({steps} step(s) from {_pendingCarouselCurrentIdx}).");
        if (steps > 0) { _pendingCarousel.IncrementIndex(steps); }
    }

    private void SelectMultiOption(int target)
    {
        if (_pendingMultiOptions == null || target >= _pendingMultiOptions.Count) { return; }
        Plugin.Log.LogInfo($"[PagedDecisionDriver] MultiChoice → option {target}.");

        // OnValueChanged(true) only AddSelection()s our target — it does NOT route through the
        // ToggleGroup, so a radio page's pre-selected default option is never removed from
        // selectedOptions (the list OnFinishConfirmed commits), silently overriding our choice.
        // For radios, deselect every option first (OnValueChanged(false) → RemoveSelection, the
        // same call a real radio switch makes) so only the target remains. Checkbox pages keep the
        // additive behaviour to preserve any minimum-selection requirement.
        if (_pendingMultiIsRadio)
        {
            for (int i = 0; i < _pendingMultiOptions.Count; i++)
            {
                if (_pendingMultiOptions[i] != null) { _pendingMultiOptions[i].OnValueChanged(false); }
            }
        }

        _pendingMultiOptions[target].OnValueChanged(true);
    }

    private void AdvancePage()
    {
        int currentPage, numPages;
        unsafe
        {
            currentPage = *(int*)(_panel.Pointer + 0xE4);
            numPages = *(int*)(_panel.Pointer + 0xE8);
        }

        if (currentPage < numPages - 1)
        {
            Plugin.Log.LogInfo($"[PagedDecisionDriver] Advancing to page [{currentPage + 2}/{numPages}].");
            _panel.IncrementPageIndex(1);
            _panel.SetNextAndFinishButtonStates();
        }
        else
        {
            Plugin.Log.LogInfo("[PagedDecisionDriver] All pages done — submitting.");
            _panel.OnFinish();
        }
    }

    private void SetAdvanceTimer()
    {
        _readyToAdvance = true;
        _advanceAt = Time.time + 0.5f;
    }

    private void ResetState()
    {
        _processedPage = -1;
        _readyToAdvance = false;
        _aiTask = null;
        _aiMultiTask = null;
        _pendingCarousel = null;
        _pendingMultiOptions = null;
        _pendingOptions = null;
        _pendingQuestion = "";
    }

    // The description every option carries verbatim, or null when they differ or there is nothing to
    // share. A one-option carousel duplicates nothing, so its description stays on the option.
    private static string SharedDescription(List<string> descs)
    {
        if (descs.Count < 2) { return null; }
        string first = descs[0];
        if (string.IsNullOrWhiteSpace(first)) { return null; }
        for (int i = 1; i < descs.Count; i++)
        {
            if (!string.Equals(descs[i], first, StringComparison.Ordinal)) { return null; }
        }
        return first;
    }

    private static unsafe string ReadTMP(nint fieldAddress)
    {
        nint objPtr = *(nint*)fieldAddress;
        if (objPtr == 0) { return ""; }
        return ConversationLinePatch.StripTags(new TMPro.TextMeshProUGUI((IntPtr)objPtr).text ?? "");
    }
}
