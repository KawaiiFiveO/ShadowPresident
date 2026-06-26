using System;
using UnityEngine;

namespace ShadowPresident;

public class CharacterCustomizationDriver : MonoBehaviour
{
    public CharacterCustomizationDriver(IntPtr ptr) : base(ptr) { }

    private CharacterCustomizationPanel _panel;

    private enum State { Idle, Randomizing, Finishing }
    private State _state = State.Idle;
    private float _actionAt;

    void Update()
    {
        if (!Plugin.AutomationEnabled) { _state = State.Idle; return; }

        if (_panel == null) { _panel = FindObjectOfType<CharacterCustomizationPanel>(); }
        if (_panel == null || !_panel.IsShowing()) { _state = State.Idle; return; }

        switch (_state)
        {
            case State.Idle:
                // Panel just appeared — wait briefly for it to settle before acting.
                _actionAt = Time.time + 0.5f;
                _state = State.Randomizing;
                break;

            case State.Randomizing:
                if (Time.time < _actionAt) { return; }
                Plugin.Log.LogInfo("[CharacterCustomizationDriver] Randomizing character options.");
                _panel.OnRandomize();
                _actionAt = Time.time + 0.3f;
                _state = State.Finishing;
                break;

            case State.Finishing:
                if (Time.time < _actionAt) { return; }
                Plugin.Log.LogInfo("[CharacterCustomizationDriver] Confirming character customization.");
                _panel.OnFinish();
                _state = State.Idle;
                break;
        }
    }
}
