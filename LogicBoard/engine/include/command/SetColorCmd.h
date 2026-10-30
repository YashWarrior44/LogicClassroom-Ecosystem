#pragma once
// =============================================================================
// SetColorCmd.h — Change the stroke (and fill, if present) color of one or
// more objects. Stores per-object old colors for exact undo.
//
// Images and PPT objects are skipped by the caller (they have no meaningful
// stroke color); this command only records the objects it's given.
// =============================================================================

#include "Command.h"
#include "../math/Types.h"
#include "../scene/WhiteboardObject.h"

#include <string>
#include <vector>

namespace lb {

class Scene;

class SetColorCmd : public Command {
public:
    SetColorCmd(std::vector<UUID> ids, RGBA new_color);
    ~SetColorCmd() override = default;

    void execute(Scene& scene) override;
    void undo(Scene& scene) override;
    std::string description() const override;

private:
    std::vector<UUID> ids_;
    RGBA new_color_;
    // Captured on first execute(): per-object old stroke + fill colors.
    std::vector<RGBA> old_stroke_;
    std::vector<RGBA> old_fill_;
    std::vector<bool> had_fill_;
    bool captured_ = false;
};

} // namespace lb
