namespace ShadowPresident;

// Single entry point for "make sure the game state the AI is about to reason over is current".
//
// The three readers poll on a 30s timer, which is fine for keeping the browser panel warm but is
// not a guarantee for any individual decision: a decision that lands in the gap — the first one
// after a load, or the first one of a new turn — is sent with whatever the last poll happened to
// capture, which may be empty (readers find nothing on the main menu) or a turn stale. The model
// then reasons over a state that isn't the one it is acting on, and there is no signal in the
// transcript that it did so.
//
// Every driver calls EnsureRead() on the main thread immediately before dispatching its /decision
// task, so the stats/journal/economy blocks in the request always describe the board as it stands
// at the moment of the decision. The polling timers remain as the background refresh.
internal static class GameState
{
    // Main thread only — the readers walk Il2Cpp lists, which is unsafe off-thread.
    internal static void EnsureRead()
    {
        GameStateReader.ReadNow();
        JournalReader.ReadNow();
        GraphReader.ReadNow();
    }
}
