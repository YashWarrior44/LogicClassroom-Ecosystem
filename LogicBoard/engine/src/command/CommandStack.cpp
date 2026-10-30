// =============================================================================
// CommandStack.cpp — Linear undo/redo history with depth and memory caps.
// =============================================================================

#include "../../include/command/CommandStack.h"
#include "../../include/scene/Scene.h"

namespace lb {

// =============================================================================
// Construction
// =============================================================================

CommandStack::CommandStack()
    : max_depth_(200), max_memory_(0), current_memory_(0) {}

CommandStack::CommandStack(size_t max_depth)
    : max_depth_(max_depth < 1 ? 1 : max_depth),
      max_memory_(0), current_memory_(0) {}

// =============================================================================
// Core operations
// =============================================================================

void CommandStack::push(Scene& scene, std::unique_ptr<Command> cmd) {
    if (!cmd) return;

    // Snapshot the estimate BEFORE execute so we can back it out if the
    // execute throws — otherwise memory accounting drifts.
    size_t estimate = cmd->memory_estimate();

    // Execute the command first. If this throws, the local unique_ptr
    // destructs and cmd is released — memory_ was never touched, no
    // accounting drift, no leak.
    try {
        cmd->execute(scene);
    } catch (...) {
        // Scene may have been partially mutated; that's the caller's
        // problem, but our own bookkeeping stays clean.
        throw;
    }

    // Pushing a new command invalidates the entire redo future.
    clear_redo();

    current_memory_ += estimate;
    undo_stack_.push_back(std::move(cmd));

    enforce_limits();
    notify();
}

bool CommandStack::undo(Scene& scene) {
    if (undo_stack_.empty()) return false;

    auto cmd = std::move(undo_stack_.back());
    undo_stack_.pop_back();

    try {
        cmd->undo(scene);
    } catch (...) {
        // Roll the command back onto the undo stack so a retry is
        // possible and the undo/redo pair is still balanced. Without
        // this the caller loses the ability to try again AND the
        // memory the cmd was counted for is orphaned in
        // current_memory_ (leading to overreports and premature
        // trim-cascades).
        undo_stack_.push_back(std::move(cmd));
        throw;
    }

    redo_stack_.push_back(std::move(cmd));

    // Memory total doesn't change (command moved between stacks).
    notify();
    return true;
}

bool CommandStack::redo(Scene& scene) {
    if (redo_stack_.empty()) return false;

    auto cmd = std::move(redo_stack_.back());
    redo_stack_.pop_back();

    try {
        cmd->execute(scene);
    } catch (...) {
        // Same rollback logic as undo() — put the cmd back on the redo
        // stack so a retry is possible and memory stays accounted for.
        redo_stack_.push_back(std::move(cmd));
        throw;
    }

    undo_stack_.push_back(std::move(cmd));

    // Enforce the depth cap here too. Was missing — a redo that lands
    // on an already-full undo stack pushed the size over max_depth_
    // silently until the next push() ran enforce_limits(). Doesn't
    // usually crash, but does defeat the whole point of a depth cap in
    // sessions with lots of undo/redo activity.
    enforce_limits();
    notify();
    return true;
}

// =============================================================================
// Queries
// =============================================================================

std::string CommandStack::next_undo_description() const {
    if (undo_stack_.empty()) return {};
    return undo_stack_.back()->description();
}

std::string CommandStack::next_redo_description() const {
    if (redo_stack_.empty()) return {};
    return redo_stack_.back()->description();
}

const Command* CommandStack::peek_undo() const {
    if (undo_stack_.empty()) return nullptr;
    return undo_stack_.back().get();
}

const Command* CommandStack::peek_redo() const {
    if (redo_stack_.empty()) return nullptr;
    return redo_stack_.back().get();
}

// =============================================================================
// Limits
// =============================================================================

void CommandStack::set_max_depth(size_t depth) {
    max_depth_ = (depth < 1) ? 1 : depth;
    enforce_limits();
}

void CommandStack::set_max_memory(size_t bytes) {
    max_memory_ = bytes;
    enforce_limits();
}

void CommandStack::enforce_limits() {
    // Depth limit: trim oldest undo entries.
    while (undo_stack_.size() > max_depth_) {
        current_memory_ -= undo_stack_.front()->memory_estimate();
        undo_stack_.pop_front();
    }

    // Memory limit (if set): trim oldest undo entries.
    if (max_memory_ > 0) {
        while (current_memory_ > max_memory_ && !undo_stack_.empty()) {
            current_memory_ -= undo_stack_.front()->memory_estimate();
            undo_stack_.pop_front();
        }
    }
}

void CommandStack::recalc_memory() {
    current_memory_ = 0;
    for (const auto& cmd : undo_stack_) {
        current_memory_ += cmd->memory_estimate();
    }
    for (const auto& cmd : redo_stack_) {
        current_memory_ += cmd->memory_estimate();
    }
}

// =============================================================================
// Bulk operations
// =============================================================================

void CommandStack::clear() {
    undo_stack_.clear();
    redo_stack_.clear();
    current_memory_ = 0;
    notify();
}

void CommandStack::clear_redo() {
    for (const auto& cmd : redo_stack_) {
        current_memory_ -= cmd->memory_estimate();
    }
    redo_stack_.clear();
}

// =============================================================================
// Notification
// =============================================================================

void CommandStack::notify() {
    if (on_change_) {
        on_change_(can_undo(), can_redo());
    }
}

} // namespace lb