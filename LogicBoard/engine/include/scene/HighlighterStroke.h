#pragma once
// =============================================================================
// HighlighterStroke.h — A highlighter stroke: semi-transparent, rectangular
// cross-section (fixed height), composited with SourceOver blending.
//
// Structurally similar to FreehandStroke but rendered differently:
// - Each segment is a filled rectangle (not a round-cap line).
// - The height is fixed; only the horizontal path follows the input.
// - Alpha / opacity is typically low (30-50%) for a see-through highlight.
//
// JSON:
// {
//   "type": "highlighter",
//   "points": [[x0,y0], [x1,y1], ...],
//   "height": 35.0,
//   ... base properties (stroke_color carries the highlight tint,
//       opacity carries the transparency) ...
// }
// =============================================================================

#include "WhiteboardObject.h"
#include <vector>

namespace lb {

class HighlighterStroke : public WhiteboardObject {
public:
    HighlighterStroke();
    explicit HighlighterStroke(UUID id);
    ~HighlighterStroke() override = default;

    // ---- Highlight-specific properties ----

    /// The vertical thickness of the highlight band in local-space pixels.
    float height() const { return height_; }
    void set_height(float h) { height_ = (h < 1.0f) ? 1.0f : h; invalidate_cache(); }

    // ---- Point data (same API as FreehandStroke) ----

    const std::vector<Vec2f>& points() const { return points_; }
    size_t point_count() const { return points_.size(); }
    bool empty() const { return points_.empty(); }

    void reserve(size_t estimated_points);
    void add_point(Vec2f pt);
    void set_points(std::vector<Vec2f> pts);
    void pop_back();
    void clear();

    /// Simplify the polyline path (Ramer-Douglas-Peucker).
    void simplify(float epsilon = 1.5f);

    // ---- WhiteboardObject interface ----

    AABB local_bounding_box() const override;

    /// Hit-test: checks if `local_point` falls inside any segment rectangle.
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "highlighter"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    std::vector<Vec2f> points_;
    float              height_ = 35.0f;

    mutable AABB cached_bbox_;
    mutable bool bbox_dirty_ = true;

    void invalidate_cache() { bbox_dirty_ = true; }
    void recompute_bbox() const;
};

} // namespace lb