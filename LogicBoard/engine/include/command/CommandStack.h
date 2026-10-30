#pragma once
// =============================================================================
// CommandStack.h — Linear undo/redo history with depth and memory caps.
//
// Usage:
//   CommandStack stack;
//   stack.push(scene, std::make_unique<AddObjectCmd>(...));  // executes + pushes
//   stack.undo(scene);   // reverts the last command
//   stack.redo(scene);   // re-applies the last undone command
//
// Pushing a new command clears the redo stack (standard linear undo model).
// The stack enforces two independent limits:
//   - max_depth:  maximum number of commands on the undo stack.
//   - max_memory: approximate byte budget across both stacks (optional).
// When a limit is exceeded the oldest undo entries are discarded.
// =============================================================================

#include "Command.h"

#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace lb {

class Scene;  // forward

class CommandStack {
public:
    // ---- Construction ----

    CommandStack();
    explicit CommandStack(size_t max_depth);
    ~CommandStack() = default;

    CommandStack(CommandStack&&) noexcept = default;
    CommandStack& operator=(CommandStack&&) noexcept = default;
    CommandStack(const CommandStack&) = delete;
    CommandStack& operator=(const CommandStack&) = delete;

    // ---- Core operations ----

    /// Execute a command and push it onto the undo stack.
    /// Clears the entire redo stack.
    void push(Scene& scene, std::unique_ptr<Command> cmd);

    /// Undo the most recent command.  Returns false if nothing to undo.
    bool undo(Scene& scene);

    /// Redo the most recently undone command.  Returns false if nothing to redo.
    bool redo(Scene& scene);

    // ---- Queries ----

    bool can_undo() const { return !undo_stack_.empty(); }
    bool can_redo() const { return !redo_stack_.empty(); }

    size_t undo_count() const { return undo_stack_.size(); }
    size_t redo_count() const { return redo_stack_.size(); }

    /// Description of the command that would be undone next (empty if none).
    std::string next_undo_description() const;

    /// Description of the command that would be redone next (empty if none).
    std::string next_redo_description() const;

    /// Peek at the top of the undo stack (nullptr if empty).
    const Command* peek_undo() const;

    /// Peek at the top of the redo stack (nullptr if empty).
    const Command* peek_redo() const;

    // ---- Limits ----

    size_t max_depth() const { return max_depth_; }
    void set_max_depth(size_t depth);

    size_t max_memory() const { return max_memory_; }
    void set_max_memory(size_t bytes);

    /// Current approximate memory usage across both stacks.
    size_t current_memory() const { return current_memory_; }

    // ---- Bulk operations ----

    /// Clear both undo and redo stacks.
    void clear();

    /// Clear only the redo stack (useful when the scene is mutated outside
    /// the command system, invalidating future redo state).
    void clear_redo();

    // ---- Change notification (optional) ----

    /// Callback invoked after any push / undo / redo / clear.
    /// The bool arguments are (can_undo, can_redo) after the operation.
    using ChangeCallback = std::function<void(bool can_undo, bool can_redo)>;
    void set_change_callback(ChangeCallback cb) { on_change_ = std::move(cb); }

private:
    std::deque<std::unique_ptr<Command>> undo_stack_;
    std::deque<std::unique_ptr<Command>> redo_stack_;

    size_t max_depth_     = 200;
    size_t max_memory_    = 0;          // 0 = unlimited
    size_t current_memory_ = 0;

    ChangeCallback on_change_;

    void enforce_limits();
    void recalc_memory();
    void notify();
};

} // namespace lb