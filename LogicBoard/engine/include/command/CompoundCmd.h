#pragma once
// =============================================================================
// CompoundCmd.h — Groups multiple commands into a single undo/redo unit.
//
// execute(): runs all sub-commands in order (first → last).
// undo():    reverts all sub-commands in reverse order (last → first).
//
// Use cases:
//   - Delete multiple selected objects as one action.
//   - Paste a group of objects.
//   - Apply a bulk transform to a selection.
//   - Any multi-step mutation that should appear as a single entry in
//     the undo history.
// =============================================================================

#include "Command.h"

#include <cstddef>
#include <memory>
#include <string>
#include <vector>

namespace lb {

class Scene;

class CompoundCmd : public Command {
public:
    CompoundCmd();

    /// Construct with a human-readable label for the grouped action.
    explicit CompoundCmd(std::string label);

    /// Construct with a label and a pre-built list of sub-commands.
    CompoundCmd(std::string label, std::vector<std::unique_ptr<Command>> commands);

    ~CompoundCmd() override = default;

    CompoundCmd(CompoundCmd&&) noexcept = default;
    CompoundCmd& operator=(CompoundCmd&&) noexcept = default;

    // ---- Building ----

    /// Append a sub-command.  Must be called before the first execute().
    void add(std::unique_ptr<Command> cmd);

    /// Number of sub-commands.
    size_t size() const { return commands_.size(); }

    /// True if no sub-commands have been added.
    bool empty() const { return commands_.empty(); }

    /// Read-only access to a sub-command by index.
    const Command* command_at(size_t i) const;

    // ---- Label ----

    const std::string& label() const { return label_; }
    void set_label(const std::string& l) { label_ = l; }

    // ---- Command interface ----

    void execute(Scene& scene) override;
    void undo(Scene& scene) override;
    std::string description() const override;
    size_t memory_estimate() const override;

private:
    std::string                              label_;
    std::vector<std::unique_ptr<Command>>    commands_;
};

} // namespace lb