// =============================================================================
// CompoundCmd.cpp
// =============================================================================

#include "../../include/command/CompoundCmd.h"
#include "../../include/scene/Scene.h"

#include <sstream>

namespace lb {

CompoundCmd::CompoundCmd()
    : label_("Compound") {}

CompoundCmd::CompoundCmd(std::string label)
    : label_(std::move(label)) {}

CompoundCmd::CompoundCmd(std::string label, std::vector<std::unique_ptr<Command>> commands)
    : label_(std::move(label)), commands_(std::move(commands)) {}

void CompoundCmd::add(std::unique_ptr<Command> cmd) {
    if (cmd) {
        commands_.push_back(std::move(cmd));
    }
}

const Command* CompoundCmd::command_at(size_t i) const {
    if (i >= commands_.size()) return nullptr;
    return commands_[i].get();
}

void CompoundCmd::execute(Scene& scene) {
    // Execute sub-commands in forward order.
    for (auto& cmd : commands_) {
        cmd->execute(scene);
    }
}

void CompoundCmd::undo(Scene& scene) {
    // Undo sub-commands in reverse order.
    for (auto it = commands_.rbegin(); it != commands_.rend(); ++it) {
        (*it)->undo(scene);
    }
}

std::string CompoundCmd::description() const {
    if (!label_.empty()) return label_;

    std::ostringstream ss;
    ss << "Compound (" << commands_.size() << " actions)";
    return ss.str();
}

size_t CompoundCmd::memory_estimate() const {
    size_t total = 64; // overhead for the compound container itself
    for (const auto& cmd : commands_) {
        total += cmd->memory_estimate();
    }
    return total;
}

} // namespace lb