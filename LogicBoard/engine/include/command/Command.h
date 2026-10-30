#pragma once
// =============================================================================
// Command.h — Abstract base for every undoable action in the LogicBoard engine.
//
// Each concrete command encapsulates a single atomic mutation of a Scene.
// Commands store enough state to both execute (apply) and undo (revert) the
// change.  The CommandStack manages the undo/redo lifecycle.
// =============================================================================

#include <memory>
#include <string>

namespace lb {

class Scene;  // forward

class Command {
public:
    virtual ~Command() = default;

    /// Apply this command to the scene.  Called on first execution and on redo.
    virtual void execute(Scene& scene) = 0;

    /// Revert this command, restoring the scene to its state before execute().
    virtual void undo(Scene& scene) = 0;

    /// Human-readable description (for debug / UI display).
    virtual std::string description() const = 0;

    /// Byte-estimate of the memory this command holds (for budget trimming).
    /// Default returns a small fixed amount; override for commands that store
    /// large payloads (e.g. cloned objects, point arrays).
    virtual size_t memory_estimate() const { return 64; }

    // Non-copyable, movable.
    Command() = default;
    Command(const Command&) = delete;
    Command& operator=(const Command&) = delete;
    Command(Command&&) noexcept = default;
    Command& operator=(Command&&) noexcept = default;
};

} // namespace lb