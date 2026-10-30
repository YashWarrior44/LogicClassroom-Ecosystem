// =============================================================================
// ShapeObject.cpp — see ShapeObject.h. Written so that no input, however
// malformed, can crash it: no unchecked JSON access, no non-finite numbers
// kept, no division by a possibly-zero length.
// =============================================================================

#include "../../include/scene/ShapeObject.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>

using json = nlohmann::json;

namespace lb {

namespace {

float finite_or_zero(float v) { return std::isfinite(v) ? v : 0.0f; }

// A stroke width that cannot overflow anything it is multiplied into. No
// real pen is a million units wide; 1e38 (found by fuzzing) made the
// arrowhead padding infinite and the bounding box with it.
float safe_width(float w) {
    return std::min(std::max(finite_or_zero(w), 0.0f), 1.0e6f);
}

Vec2f sanitize(Vec2f v) { return Vec2f{finite_or_zero(v.x), finite_or_zero(v.y)}; }

std::string clamp_kind(const std::string& k) {
    return k.size() > ShapeObject::kMaxKindLength
        ? k.substr(0, ShapeObject::kMaxKindLength) : k;
}

// Squared distance from p to segment a-b; safe for a == b.
float seg_dist_sq(Vec2f p, Vec2f a, Vec2f b) {
    Vec2f ab = b - a;
    float len_sq = ab.length_sq();
    if (!(len_sq > 1e-12f)) return p.distance_sq(a);
    float t = (p - a).dot(ab) / len_sq;
    t = std::max(0.0f, std::min(1.0f, t));
    return p.distance_sq(a + ab * t);
}

// Reads [x, y] from j[key] if — and only if — it really is two numbers.
bool read_vec(const json& j, const char* key, Vec2f& out) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_array() || it->size() < 2) return false;
    const json& x = (*it)[0];
    const json& y = (*it)[1];
    if (!x.is_number() || !y.is_number()) return false;
    out = sanitize(Vec2f{x.get<float>(), y.get<float>()});
    return true;
}

} // namespace

ShapeObject::ShapeObject() : WhiteboardObject() {}
ShapeObject::ShapeObject(UUID id) : WhiteboardObject(id) {}

ShapeObject::ShapeObject(std::string kind, Vec2f start, Vec2f end)
    : WhiteboardObject(), kind_(clamp_kind(kind)),
      start_(sanitize(start)), end_(sanitize(end)) {}

ShapeObject::ShapeObject(std::string kind, Vec2f start, Vec2f end, UUID id)
    : WhiteboardObject(id), kind_(clamp_kind(kind)),
      start_(sanitize(start)), end_(sanitize(end)) {}

void ShapeObject::set_kind(const std::string& k) { kind_ = clamp_kind(k); }
void ShapeObject::set_start(Vec2f v) { start_ = sanitize(v); }
void ShapeObject::set_end(Vec2f v) { end_ = sanitize(v); }

bool ShapeObject::is_line_kind() const {
    // "dashed_line", "dotted_line", "double_arrow", and any future
    // "*_line" / "*_arrow".
    auto ends_with = [&](const char* suffix) {
        const std::string s(suffix);
        return kind_.size() >= s.size() &&
               kind_.compare(kind_.size() - s.size(), s.size(), s) == 0;
    };
    return ends_with("line") || ends_with("arrow");
}

AABB ShapeObject::local_bounding_box() const {
    float sw = safe_width(stroke_width());
    float pad = sw * 0.5f;
    if (is_line_kind()) {
        // An arrowhead is wider than the line: max(14, 5 * width) across,
        // the same numbers the Python renderer uses.
        pad = std::max(pad, std::max(14.0f, 5.0f * sw) * 0.5f);
    }
    float x0 = std::min(start_.x, end_.x), x1 = std::max(start_.x, end_.x);
    float y0 = std::min(start_.y, end_.y), y1 = std::max(start_.y, end_.y);
    return AABB{{x0 - pad, y0 - pad}, {x1 + pad, y1 + pad}};
}

bool ShapeObject::hit_test_local(Vec2f p, float tolerance) const {
    if (!std::isfinite(p.x) || !std::isfinite(p.y)) return false;
    float tol = std::isfinite(tolerance) ? std::max(0.0f, tolerance) : 0.0f;
    if (is_line_kind()) {
        float r = safe_width(stroke_width()) * 0.5f + std::min(tol, 1.0e6f);
        return seg_dist_sq(p, start_, end_) <= r * r;
    }
    AABB b = local_bounding_box();
    return p.x >= b.min_pt.x - tol && p.x <= b.max_pt.x + tol &&
           p.y >= b.min_pt.y - tol && p.y <= b.max_pt.y + tol;
}

std::unique_ptr<WhiteboardObject> ShapeObject::clone() const {
    auto copy = std::make_unique<ShapeObject>(kind_, start_, end_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    return copy;
}

std::unique_ptr<WhiteboardObject> ShapeObject::clone_exact() const {
    auto copy = std::make_unique<ShapeObject>(kind_, start_, end_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    return copy;
}

void ShapeObject::serialize_properties(json& j) const {
    j["kind"] = kind_;
    j["start"] = json::array({start_.x, start_.y});
    j["end"] = json::array({end_.x, end_.y});
}

void ShapeObject::deserialize_properties(const json& j) {
    auto it = j.find("kind");
    if (it != j.end() && it->is_string()) {
        kind_ = clamp_kind(it->get<std::string>());
    }
    read_vec(j, "start", start_);
    read_vec(j, "end", end_);
}

} // namespace lb
