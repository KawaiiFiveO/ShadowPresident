using System;
using System.Runtime.InteropServices;
using UnityEngine;
using UnityEngine.InputSystem;

namespace ShadowPresident;

// Throttles rendering when the window loses focus (the game logic keeps running
// because BackgroundExecutionPatch + AlwaysFocusedPatch prevent any game pauses).
// OnApplicationFocus fires from actual OS events, unaffected by the isFocused spoof.
public class BackgroundController : MonoBehaviour
{
    public BackgroundController(IntPtr ptr) : base(ptr) { }

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    // Find the Unity render window by its registered class name, ignoring the BepInEx console.
    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    private static extern IntPtr FindWindow(string lpClassName, string lpWindowName);

    private const int SW_MINIMIZE = 6;

    // Cached on first access; Unity registers its window as "UnityWndClass".
    private static IntPtr _gameWindow = IntPtr.Zero;
    private static IntPtr GameWindow
    {
        get
        {
            if (_gameWindow == IntPtr.Zero)
                _gameWindow = FindWindow("UnityWndClass", null);
            return _gameWindow;
        }
    }

    private int _normalFps;
    private bool _isFocused = true;
    private bool _isThrottled = false;
    private float _savedVolume = 1f;
    private bool _isMuted = false;

    void Awake()
    {
        // Capture whatever FPS limit the game has set before we touch anything.
        _normalFps = Application.targetFrameRate;
        Plugin.Log.LogInfo($"[Background] Game FPS target: {(_normalFps <= 0 ? "unlimited" : _normalFps.ToString())}");
    }

    void Update()
    {
        if (Keyboard.current != null &&
            Keyboard.current[InputHelper.ToInputKey(Plugin.MinimizeKey.Value)].wasPressedThisFrame)
        {
            ShowWindow(GameWindow, SW_MINIMIZE);
        }

        // Throttle only when the window is actually minimized, not merely unfocused.
        if (!_isFocused)
        {
            bool minimized = IsIconic(GameWindow);
            if (minimized && !_isThrottled)
            {
                Application.targetFrameRate = Plugin.BackgroundFps.Value;
                _isThrottled = true;
                Plugin.Log.LogInfo($"[Background] Minimized — throttling to {Plugin.BackgroundFps.Value} FPS");
            }
            else if (!minimized && _isThrottled)
            {
                Application.targetFrameRate = _normalFps;
                _isThrottled = false;
                Plugin.Log.LogInfo("[Background] Unminimized (unfocused) — restoring FPS");
            }
        }
    }

    void OnApplicationFocus(bool hasFocus)
    {
        _isFocused = hasFocus;

        if (!hasFocus)
        {
            // Re-assert runInBackground each time focus is lost — Suzerain may reset it,
            // which would cause OnApplicationPause to fire when minimized and stall DOTween.
            Application.runInBackground = true;
        }
        if (hasFocus)
        {
            if (_isThrottled)
            {
                Application.targetFrameRate = _normalFps;
                _isThrottled = false;
                Plugin.Log.LogInfo($"[Background] Focused — restoring FPS to {(_normalFps <= 0 ? "unlimited" : _normalFps.ToString())}");
            }
            if (_isMuted)
            {
                AudioListener.volume = _savedVolume;
                _isMuted = false;
                Plugin.Log.LogInfo("[Background] Focused — audio restored");
            }
        }
        else
        {
            _savedVolume = AudioListener.volume;
            AudioListener.volume = 0f;
            _isMuted = true;
            Plugin.Log.LogInfo("[Background] Unfocused — audio muted");
        }
    }
}
