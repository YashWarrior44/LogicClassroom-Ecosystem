#pragma once
// =============================================================================
// Types.h — Fundamental value types for the LogicBoard engine.
//
// Lightweight structs with no heap allocation. Designed for pass-by-value
// and direct embedding inside shape objects.
// =============================================================================

#include <cstdint>
#include <cmath>
#include <algorithm>
#include <string>

namespace lb {

// -----------------------------------------------------------------------------
// Vec2f — 2D point / vector (float precision)
// -----------------------------------------------------------------------------
struct Vec2f {
    float x = 0.0f;
    float y = 0.0f;

    constexpr Vec2f() = default;
    constexpr Vec2f(float x_, float y_) : x(x_), y(y_) {}

    constexpr Vec2f operator+(const Vec2f& o) const { return {x + o.x, y + o.y}; }
    constexpr Vec2f operator-(const Vec2f& o) const { return {x - o.x, y - o.y}; }
    constexpr Vec2f operator*(float s) const { return {x * s, y * s}; }
    constexpr Vec2f operator/(float s) const { return {x / s, y / s}; }

    constexpr Vec2f& operator+=(const Vec2f& o) { x += o.x; y += o.y; return *this; }
    constexpr Vec2f& operator-=(const Vec2f& o) { x -= o.x; y -= o.y; return *this; }
    constexpr Vec2f& operator*=(float s) { x *= s; y *= s; return *this; }

    constexpr float dot(const Vec2f& o) const { return x * o.x + y * o.y; }
    constexpr float cross(const Vec2f& o) const { return x * o.y - y * o.x; }
    constexpr float length_sq() const { return x * x + y * y; }
    float length() const { return std::sqrt(length_sq()); }

    Vec2f normalized() const {
        float len = length();
        if (len < 1e-8f) return {0.0f, 0.0f};
        return {x / len, y / len};
    }

    /// Perpendicular vector (rotated 90° counter-clockwise).
    constexpr Vec2f perp() const { return {-y, x}; }

    constexpr float distance_sq(const Vec2f& o) const { return (*this - o).length_sq(); }
    float distance(const Vec2f& o) const { return (*this - o).length(); }

    constexpr bool operator==(const Vec2f& o) const { return x == o.x && y == o.y; }
    constexpr bool operator!=(const Vec2f& o) const { return !(*this == o); }
};

inline constexpr Vec2f operator*(float s, const Vec2f& v) { return v * s; }

// -----------------------------------------------------------------------------
// RGBA — Color with alpha (0-255 per channel)
// -----------------------------------------------------------------------------
struct RGBA {
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;
    uint8_t a = 255;

    constexpr RGBA() = default;
    constexpr RGBA(uint8_t r_, uint8_t g_, uint8_t b_, uint8_t a_ = 255)
        : r(r_), g(g_), b(b_), a(a_) {}

    /// Construct from 0xRRGGBB integer (alpha defaults to 255).
    static constexpr RGBA from_hex(uint32_t hex, uint8_t alpha = 255) {
        return RGBA{
            static_cast<uint8_t>((hex >> 16) & 0xFF),
            static_cast<uint8_t>((hex >> 8)  & 0xFF),
            static_cast<uint8_t>( hex        & 0xFF),
            alpha
        };
    }

    /// Returns "#RRGGBB" or "#RRGGBBAA" string.
    std::string to_hex_string() const {
        char buf[10];
        if (a == 255) {
            std::snprintf(buf, sizeof(buf), "#%02x%02x%02x", r, g, b);
        } else {
            std::snprintf(buf, sizeof(buf), "#%02x%02x%02x%02x", r, g, b, a);
        }
        return std::string(buf);
    }

    constexpr bool operator==(const RGBA& o) const {
        return r == o.r && g == o.g && b == o.b && a == o.a;
    }
    constexpr bool operator!=(const RGBA& o) const { return !(*this == o); }

    // Common preset colors
    static constexpr RGBA black()       { return {0, 0, 0, 255}; }
    static constexpr RGBA white()       { return {255, 255, 255, 255}; }
    static constexpr RGBA transparent() { return {0, 0, 0, 0}; }
    static constexpr RGBA blue()        { return {0, 122, 255, 255}; }
    static constexpr RGBA red()         { return {255, 59, 48, 255}; }
    static constexpr RGBA green()       { return {52, 199, 89, 255}; }
    static constexpr RGBA orange()      { return {255, 170, 0, 255}; }
};

// -----------------------------------------------------------------------------
// AABB — Axis-Aligned Bounding Box
// -----------------------------------------------------------------------------
struct AABB {
    Vec2f min_pt;   // top-left
    Vec2f max_pt;   // bottom-right

    constexpr AABB() = default;
    constexpr AABB(Vec2f min_, Vec2f max_) : min_pt(min_), max_pt(max_) {}

    static constexpr AABB from_center_size(Vec2f center, Vec2f half) {
        return {center - half, center + half};
    }

    static AABB from_points(const Vec2f* pts, size_t count) {
        if (count == 0) return {};
        float xmin = pts[0].x, xmax = pts[0].x;
        float ymin = pts[0].y, ymax = pts[0].y;
        for (size_t i = 1; i < count; ++i) {
            xmin = std::min(xmin, pts[i].x);
            xmax = std::max(xmax, pts[i].x);
            ymin = std::min(ymin, pts[i].y);
            ymax = std::max(ymax, pts[i].y);
        }
        return {{xmin, ymin}, {xmax, ymax}};
    }

    constexpr float width()  const { return max_pt.x - min_pt.x; }
    constexpr float height() const { return max_pt.y - min_pt.y; }
    constexpr Vec2f center() const { return (min_pt + max_pt) * 0.5f; }
    constexpr Vec2f size()   const { return max_pt - min_pt; }
    constexpr float area()   const { return width() * height(); }

    constexpr bool contains(Vec2f p) const {
        return p.x >= min_pt.x && p.x <= max_pt.x
            && p.y >= min_pt.y && p.y <= max_pt.y;
    }

    constexpr bool intersects(const AABB& o) const {
        return min_pt.x <= o.max_pt.x && max_pt.x >= o.min_pt.x
            && min_pt.y <= o.max_pt.y && max_pt.y >= o.min_pt.y;
    }

    /// Grow this box to include another box.
    constexpr AABB merged(const AABB& o) const {
        return {
            {std::min(min_pt.x, o.min_pt.x), std::min(min_pt.y, o.min_pt.y)},
            {std::max(max_pt.x, o.max_pt.x), std::max(max_pt.y, o.max_pt.y)}
        };
    }

    /// Expand the box by a uniform margin on all sides.
    constexpr AABB expanded(float margin) const {
        return {{min_pt.x - margin, min_pt.y - margin},
                {max_pt.x + margin, max_pt.y + margin}};
    }

    constexpr bool is_valid() const {
        return max_pt.x >= min_pt.x && max_pt.y >= min_pt.y;
    }
};

// -----------------------------------------------------------------------------
// LineCap / LineJoin — Stroke end and corner styles
// -----------------------------------------------------------------------------
enum class LineCap : uint8_t {
    Butt   = 0,
    Round  = 1,
    Square = 2,
};

enum class LineJoin : uint8_t {
    Miter = 0,
    Round = 1,
    Bevel = 2,
};

// -----------------------------------------------------------------------------
// TextAlign / TextVAlign — (legacy enums, retained for ABI; no longer used)
// -----------------------------------------------------------------------------
enum class TextAlign : uint8_t {
    Left   = 0,
    Center = 1,
    Right  = 2,
};

enum class TextVAlign : uint8_t {
    Top    = 0,
    Middle = 1,
    Bottom = 2,
};

} // namespace lb