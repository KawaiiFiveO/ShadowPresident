using System;
using UnityEngine;

namespace ShadowPresident;

public class AIOverlay : MonoBehaviour
{
    public AIOverlay(IntPtr ptr) : base(ptr) { }

    private static AIOverlay _instance;

    private enum State { Hidden, Thinking, Reasoning, Error, Pending }
    private State _state = State.Hidden;
    private string _message = "";
    private float _hideAt = 0f;
    private GUIStyle _style;

    void Awake() => _instance = this;

    void Update()
    {
        if ((_state == State.Reasoning || _state == State.Error) && Time.time > _hideAt)
            _state = State.Hidden;
    }

    void OnGUI()
    {
        if (_state == State.Hidden) { return; }

        if (_style == null)
        {
            _style = new GUIStyle(GUI.skin.box)
            {
                fontSize = 18,
                wordWrap = true,
                alignment = TextAnchor.MiddleLeft,
            };
            _style.normal.textColor = Color.white;
            _style.padding = new RectOffset(12, 12, 8, 8);
        }

        string name = AIClient.ModelName;
        string tokens = AIClient.LastPromptTokens > 0
            ? $"  [{AIClient.LastPromptTokens} in / {AIClient.LastCompletionTokens} out]"
            : "";
        string text = _state switch
        {
            State.Thinking => $"{name} is thinking...{tokens}",
            State.Reasoning => $"{name}{tokens}: {_message}",
            State.Error => $"{name} error: {_message}",
            State.Pending => _message,
            _ => "",
        };

        float width = Mathf.Min(Screen.width - 40, 800);
        float height = 90;
        GUI.Box(new Rect(20, Screen.height - height - 20, width, height), text, _style);
    }

    // Only shows if nothing is currently displayed — reasoning takes priority
    public static void ShowThinking()
    {
        if (_instance == null) { return; }
        if (_instance._state == State.Reasoning) { return; }
        _instance._state = State.Thinking;
        _instance._message = "";
    }

    // Replaces whatever is showing. Resets the idle timeout.
    // No-op if reasoning is empty — leaves the previous message visible.
    public static void ShowReasoning(string reasoning)
    {
        if (_instance == null || string.IsNullOrWhiteSpace(reasoning)) { return; }
        _instance._state = State.Reasoning;
        _instance._message = reasoning;
        _instance._hideAt = Time.time + Plugin.OverlayDuration.Value;
    }

    public static void ShowError(string message)
    {
        if (_instance == null) { return; }
        _instance._state = State.Error;
        _instance._message = message;
        _instance._hideAt = Time.time + Plugin.OverlayDuration.Value;
    }

    public static void ShowPending(string message)
    {
        if (_instance == null) { return; }
        _instance._state = State.Pending;
        _instance._message = message;
    }

    public static void Hide()
    {
        if (_instance == null) { return; }
        _instance._state = State.Hidden;
    }
}
