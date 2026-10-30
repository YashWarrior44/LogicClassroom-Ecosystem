// =============================================================================
// HighlighterStroke.cpp — Semi-transparent highlighter stroke rendered as
// rectangular segments of fixed height.  Shares the same polyline path
// structure as FreehandStroke but with rectangular cross-section geometry
// for bounding box and hit-testing.
// =============================================================================

#include "../../include/scene/HighlighterStroke.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <stack>
#include <utility>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// Construction
// =============================================================================

HighlighterStroke::HighlighterStroke()
    : WhiteboardObject() {
    // Highlighter defaults: orange tint, 30% opacity, no fill.
    set_stroke_color(RGBA::orange());
    set_opacity(0.30f);
    set_line_cap(LineCap::Butt);
    set_line_join(LineJoin::Miter);
}

HighlighterStroke::HighlighterStroke(UUID id)
    : WhiteboardObject(id) {
    set_stroke_color(RGBA::orange());
    set_opacity(0.30f);
    set_line_cap(LineCap::Butt);
    set_line_join(LineJoin::Miter);
}

// =============================================================================
// Point mutation
// =============================================================================

void HighlighterStroke::reserve(size_t estimated_points) {
    points_.reserve(estimated_points);
}

void HighlighterStroke::add_point(Vec2f pt) {
    points_.push_back(pt);
    invalidate_cache();
}

void HighlighterStroke::set_points(std::vector<Vec2f> pts) {
    points_ = std::move(pts);
    invalidate_cache();
}

void HighlighterStroke::pop_back() {
    if (points_.empty()) return;
    points_.pop_back();
    invalidate_cache();
}

void HighlighterStroke::clear() {
    points_.clear();
    invalidate_cache();
}

// =============================================================================
// Ramer-Douglas-Peucker simplification (same algorithm as FreehandStroke)
// =============================================================================

namespace {

float hl_perp_distance(Vec2f p, Vec2f a, Vec2f b) {
    Vec2f ab = b - a;
    float len_sq = ab.length_sq();
    if (len_sq < 1e-12f) return p.distance(a);
    float t = (p - a).dot(ab) / len_sq;
    t = std::max(0.0f, std::min(1.0f, t));
    Vec2f proj = a + ab * t;
    return p.distance(proj);
}

std::vector<bool> hl_rdp_mask(const std::vector<Vec2f>& pts, float epsilon) {
    size_t n = pts.size();
    std::vector<bool> keep(n, false);

    // A tolerance below zero is not a finer simplification, it is a hang.
    //
    // On a span whose points are all collinear every perpendicular distance
    // is 0, so max_dist stays 0 and max_idx is never moved off `start`. The
    // split test then reads `0 > -1` -- true -- and pushes {start, max_idx}
    // and {max_idx, end}, which is {start, start} (discarded) and
    // {start, end}: the very range just popped. The stack never shrinks and
    // the call never returns. A straight line simplified with a negative
    // epsilon froze the process outright.
    //
    // Written as `!(epsilon > 0)` rather than `epsilon < 0` so NaN -- where
    // every comparison is false -- lands here too instead of falling through
    // into the same trap.
    if (!(epsilon > 0.0f)) epsilon = 0.0f;

    if (n < 3) {
        std::fill(keep.begin(), keep.end(), true);
        return keep;
    }

    keep[0] = true;
    keep[n - 1] = true;

    std::stack<std::pair<size_t, size_t>> stack;
    stack.push({0, n - 1});

    while (!stack.empty()) {
        auto [start, end] = stack.top();
        stack.pop();

        if (end <= start + 1) continue;

        float max_dist = 0.0f;
        size_t max_idx = start;

        for (size_t i = start + 1; i < end; ++i) {
            float d = hl_perp_distance(pts[i], pts[start], pts[end]);
            if (d > max_dist) {
                max_dist = d;
                max_idx = i;
            }
        }

        // The index must be strictly INSIDE the span, or the two halves are
        // not smaller than the whole. Clamping epsilon above already rules
        // out the case that broke this, but termination should not rest on
        // an argument about the tolerance -- this holds for any input.
        if (max_dist > epsilon && max_idx > start && max_idx < end) {
            keep[max_idx] = true;
            stack.push({start, max_idx});
            stack.push({max_idx, end});
        }
    }

    return keep;
}

} // anonymous namespace

void HighlighterStroke::simplify(float epsilon) {
    if (points_.size() < 3) return;

    std::vector<bool> keep = hl_rdp_mask(points_, epsilon);

    std::vector<Vec2f> new_points;
    new_points.reserve(points_.size());

    for (size_t i = 0; i < points_.size(); ++i) {
        if (keep[i]) {
            new_points.push_back(points_[i]);
        }
    }

    points_ = std::move(new_points);
    invalidate_cache();
}

// =============================================================================
// Bounding box
//
// Each segment is a rectangle of the path's height centered vertically on
// the polyline.  The bounding box is the union of all segment rectangles,
// which equals the polyline AABB expanded by half the highlighter height.
// =============================================================================

void HighlighterStroke::recompute_bbox() const {
    if (points_.empty()) {
        cached_bbox_ = AABB();
        bbox_dirty_ = false;
        return;
    }

    cached_bbox_ = AABB::from_points(points_.data(), points_.size());

    // Expand vertically by half the highlight height, and horizontally by
    // a tiny margin for the segment width (single-pixel-wide rectangles).
    float half_h = height_ * 0.5f;
    cached_bbox_ = AABB{
        {cached_bbox_.min_pt.x - 1.0f, cached_bbox_.min_pt.y - half_h},
        {cached_bbox_.max_pt.x + 1.0f, cached_bbox_.max_pt.y + half_h}
    };

    bbox_dirty_ = false;
}

AABB HighlighterStroke::local_bounding_box() const {
    if (bbox_dirty_) recompute_bbox();
    return cached_bbox_;
}

// =============================================================================
// Hit-test
//
// The highlighter renders each segment as a filled rectangle whose long axis
// follows the path and whose short axis is `height_`.  For hit-testing we
// check whether the point falls inside any segment rectangle.
//
// For a segment from A to B:
//   1. Project the test point onto the segment axis and check that the
//      projection falls within [0, segment_length].
//   2. Compute the perpendicular distance and check that it is within
//      half the height (plus tolerance).
//
// For a single-point stroke the rectangle degenerates to a vertical bar
// of width 1 and height `height_`.
// =============================================================================

namespace {

// Squared distance from point P to line segment A–B (reused for fallback).
float hl_point_seg_dist_sq(Vec2f p, Vec2f a, Vec2f b) {
    Vec2f ab = b - a;
    float len_sq = ab.length_sq();
    if (len_sq < 1e-12f) return p.distance_sq(a);
    float t = (p - a).dot(ab) / len_sq;
    t = std::max(0.0f, std::min(1.0f, t));
    Vec2f proj = a + ab * t;
    return p.distance_sq(proj);
}

// Check if point P is inside the oriented rectangle defined by segment A–B
// with half-height `half_h`, expanded by `tolerance`.
bool point_in_segment_rect(Vec2f p, Vec2f a, Vec2f b, float half_h, float tolerance) {
    Vec2f ab = b - a;
    float seg_len_sq = ab.length_sq();

    if (seg_len_sq < 1e-12f) {
        // Degenerate segment — test against a small rect around A.
        return std::fabs(p.x - a.x) <= (1.0f + tolerance)
            && std::fabs(p.y - a.y) <= (half_h + tolerance);
    }

    float seg_len = std::sqrt(seg_len_sq);
    Vec2f axis = ab / seg_len;          // unit vector along segment
    Vec2f perp = axis.perp();           // unit vector perpendicular

    Vec2f ap = p - a;
    float along = ap.dot(axis);         // projection onto segment axis
    float across = ap.dot(perp);        // perpendicular distance

    // Along must be in [0, seg_len], across must be in [-half_h, +half_h],
    // both expanded by tolerance.
    return along >= -tolerance
        && along <= seg_len + tolerance
        && across >= -(half_h + tolerance)
        && across <=  (half_h + tolerance);
}

} // anonymous namespace

bool HighlighterStroke::hit_test_local(Vec2f local_point, float tolerance) const {
    if (points_.empty()) return false;

    // Quick AABB rejection.
    AABB bb = local_bounding_box();
    if (!bb.expanded(tolerance).contains(local_point)) return false;

    float half_h = height_ * 0.5f;

    // Single-point: vertical bar.
    if (points_.size() == 1) {
        return std::fabs(local_point.x - points_[0].x) <= (1.0f + tolerance)
            && std::fabs(local_point.y - points_[0].y) <= (half_h + tolerance);
    }

    // Check each segment rectangle.
    for (size_t i = 0; i + 1 < points_.size(); ++i) {
        if (point_in_segment_rect(local_point, points_[i], points_[i + 1],
                                  half_h, tolerance)) {
            return true;
        }
    }

    return false;
}

// =============================================================================
// Clone
// =============================================================================

std::unique_ptr<WhiteboardObject> HighlighterStroke::clone() const {
    auto copy = std::make_unique<HighlighterStroke>();
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    copy->points_ = points_;
    copy->height_ = height_;
    copy->invalidate_cache();
    return copy;
}

std::unique_ptr<WhiteboardObject> HighlighterStroke::clone_exact() const {
    auto copy = std::make_unique<HighlighterStroke>(id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    copy->points_ = points_;
    copy->height_ = height_;
    copy->invalidate_cache();
    return copy;
}

// =============================================================================
// JSON serialization
// =============================================================================

void HighlighterStroke::serialize_properties(json& j) const {
    json pts = json::array();
    pts.get_ref<json::array_t&>().reserve(points_.size());
    for (const auto& p : points_) {
        pts.push_back(json::array({p.x, p.y}));
    }
    j["points"] = std::move(pts);
    j["height"] = height_;
}

void HighlighterStroke::deserialize_properties(const json& j) {
    points_.clear();

    if (j.contains("points") && j["points"].is_array()) {
        const auto& arr = j["points"];
        points_.reserve(arr.size());
        for (const auto& pt : arr) {
            if (pt.is_array() && pt.size() >= 2) {
                points_.push_back({pt[0].get<float>(), pt[1].get<float>()});
            }
        }
    }

    if (j.contains("height") && j["height"].is_number()) {
        height_ = j["height"].get<float>();
        if (height_ < 1.0f) height_ = 1.0f;
    }

    invalidate_cache();
}

} // namespace lb