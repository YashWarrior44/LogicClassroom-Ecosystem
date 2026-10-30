#include <gtest/gtest.h>
#include <scene/FreehandStroke.h>
#include <scene/RectObject.h>
#include <scene/EllipseObject.h>
#include <scene/LineObject.h>

using namespace lb;

TEST(HitTestTest, FreehandStroke) {
    FreehandStroke stroke;
    stroke.set_stroke_width(4.0f);
    stroke.add_point({0, 0});
    stroke.add_point({100, 0});

    EXPECT_TRUE(stroke.hit_test_local({50, 0}, 2.0f));
    EXPECT_TRUE(stroke.hit_test_local({50, 3}, 2.0f));
    EXPECT_FALSE(stroke.hit_test_local({50, 50}, 2.0f));
}

TEST(HitTestTest, RectFilled) {
    RectObject rect({10, 10}, {100, 50});
    rect.set_fill_color(RGBA::blue());

    EXPECT_TRUE(rect.hit_test_local({60, 35}, 0.0f));
    EXPECT_FALSE(rect.hit_test_local({200, 200}, 0.0f));
}

TEST(HitTestTest, EllipseStroke) {
    EllipseObject ell({100, 100}, {50, 30});
    ell.set_stroke_width(3.0f);

    EXPECT_TRUE(ell.hit_test_local({150, 100}, 4.0f));
    EXPECT_TRUE(ell.hit_test_local({100, 130}, 4.0f));
    EXPECT_FALSE(ell.hit_test_local({100, 100}, 4.0f));
}

TEST(HitTestTest, Line) {
    LineObject line({0, 0}, {100, 0});
    line.set_stroke_width(4.0f);

    EXPECT_TRUE(line.hit_test_local({50, 0}, 2.0f));
    EXPECT_FALSE(line.hit_test_local({50, 50}, 2.0f));
}
