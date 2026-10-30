#pragma once
// =============================================================================
// SetBackgroundCmd.h — Set or clear a page's background image (undoable).
// Stores the previous bg_image src so undo restores it exactly.
// =============================================================================

#include "Command.h"
#include <string>

namespace lb {

class Scene;

class SetBackgroundCmd : public Command {
public:
    explicit SetBackgroundCmd(std::string new_src);
    ~SetBackgroundCmd() override = default;

    void execute(Scene& scene) override;
    void undo(Scene& scene) override;
    std::string description() const override;

private:
    std::string new_src_;
    std::string old_src_;
    bool captured_ = false;
};

} // namespace lb
