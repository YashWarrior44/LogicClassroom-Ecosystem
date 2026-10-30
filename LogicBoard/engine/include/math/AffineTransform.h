#pragma once
// =============================================================================
// AffineTransform.h — 2D affine transformation matrix (3×3, row-major).
//
// Layout (row-major, last row always [0 0 1]):
//   | m[0] m[1] m[2] |     | a  b  tx |
//   | m[3] m[4] m[5] |  =  | c  d  ty |
//   |  0    0    1   |     | 0  0   1 |
// =============================================================================

#include "Types.h"
#include <array>
#include <cmath>

namespace lb {

struct AffineTransform {
    // m[0]=a  m[1]=b  m[2]=tx
    // m[3]=c  m[4]=d  m[5]=ty
    std::array<float, 6> m = {1, 0, 0,
                               0, 1, 0};

    constexpr AffineTransform() = default;

    constexpr AffineTransform(float a, float b, float tx,
                               float c, float d, float ty)
        : m{a, b, tx, c, d, ty} {}

    // Named accessors
    constexpr float a()  const { return m[0]; }
    constexpr float b()  const { return m[1]; }
    constexpr float tx() const { return m[2]; }
    constexpr float c()  const { return m[3]; }
    constexpr float d()  const { return m[4]; }
    constexpr float ty() const { return m[5]; }

    // ---- Factory constructors ----

    static constexpr AffineTransform identity() { return {}; }

    static constexpr AffineTransform translation(float dx, float dy) {
        return {1, 0, dx,
                0, 1, dy};
    }

    static constexpr AffineTransform scaling(float sx, float sy) {
        return {sx, 0, 0,
                0, sy, 0};
    }

    static constexpr AffineTransform uniform_scale(float s) {
        return scaling(s, s);
    }

    static AffineTransform rotation(float radians) {
        float cs = std::cos(radians);
        float sn = std::sin(radians);
        return {cs, -sn, 0,
                sn,  cs, 0};
    }

    /// Rotate around an arbitrary pivot point.
    static AffineTransform rotation_around(float radians, Vec2f pivot) {
        return translation(pivot.x, pivot.y)
            .then(rotation(radians))
            .then(translation(-pivot.x, -pivot.y));
    }

    // ---- Point transformation ----

    constexpr Vec2f apply(Vec2f p) const {
        return {
            m[0] * p.x + m[1] * p.y + m[2],
            m[3] * p.x + m[4] * p.y + m[5]
        };
    }

    /// Transform a direction vector (ignores translation).
    constexpr Vec2f apply_direction(Vec2f v) const {
        return {
            m[0] * v.x + m[1] * v.y,
            m[3] * v.x + m[4] * v.y
        };
    }

    // ---- Composition: this * other  (apply other first, then this) ----

    constexpr AffineTransform then(const AffineTransform& o) const {
        return {
            m[0]*o.m[0] + m[1]*o.m[3],
            m[0]*o.m[1] + m[1]*o.m[4],
            m[0]*o.m[2] + m[1]*o.m[5] + m[2],

            m[3]*o.m[0] + m[4]*o.m[3],
            m[3]*o.m[1] + m[4]*o.m[4],
            m[3]*o.m[2] + m[4]*o.m[5] + m[5]
        };
    }

    /// Prepend a translation to this transform.
    constexpr AffineTransform translated(float dx, float dy) const {
        return then(translation(dx, dy));
    }

    /// Prepend a scale to this transform.
    constexpr AffineTransform scaled(float sx, float sy) const {
        return then(scaling(sx, sy));
    }

    /// Prepend a rotation to this transform.
    AffineTransform rotated(float radians) const {
        return then(rotation(radians));
    }

    // ---- Inverse ----

    AffineTransform inverse() const {
        float det = m[0] * m[4] - m[1] * m[3];
        if (std::fabs(det) < 1e-12f) {
            return identity();  // degenerate — return identity as safe fallback
        }
        float inv_det = 1.0f / det;
        return {
             m[4] * inv_det,
            -m[1] * inv_det,
            (m[1] * m[5] - m[4] * m[2]) * inv_det,

            -m[3] * inv_det,
             m[0] * inv_det,
            (m[3] * m[2] - m[0] * m[5]) * inv_det
        };
    }

    // ---- Queries ----

    constexpr bool is_identity() const {
        return m[0] == 1.0f && m[1] == 0.0f && m[2] == 0.0f
            && m[3] == 0.0f && m[4] == 1.0f && m[5] == 0.0f;
    }

    /// Transform an AABB — returns the bounding box of the 4 transformed corners.
    AABB apply(const AABB& box) const {
        Vec2f corners[4] = {
            apply(box.min_pt),
            apply({box.max_pt.x, box.min_pt.y}),
            apply(box.max_pt),
            apply({box.min_pt.x, box.max_pt.y}),
        };
        return AABB::from_points(corners, 4);
    }

    bool operator==(const AffineTransform& o) const { return m == o.m; }
    bool operator!=(const AffineTransform& o) const { return m != o.m; }
};

} // namespace lb