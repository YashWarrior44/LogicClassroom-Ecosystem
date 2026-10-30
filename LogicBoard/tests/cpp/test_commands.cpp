#include <gtest/gtest.h>
#include <command/CommandStack.h>
#include <command/StrokeDrawCmd.h>
#include <command/AddObjectCmd.h>
#include <command/DeleteObjectCmd.h>
#include <scene/Scene.h>
#include <scene/FreehandStroke.h>

using namespace lb;

TEST(CommandStackTest, PushUndoRedo) {
    Scene scene(800, 600);
    CommandStack stack(100);

    auto stroke = std::make_unique<FreehandStroke>();
    stroke->add_point({0, 0});
    stroke->add_point({100, 100});

    stack.push(scene, std::make_unique<StrokeDrawCmd>(std::move(stroke)));
    EXPECT_EQ(scene.object_count(), 1);
    EXPECT_TRUE(stack.can_undo());

    stack.undo(scene);
    EXPECT_EQ(scene.object_count(), 0);
    EXPECT_TRUE(stack.can_redo());

    stack.redo(scene);
    EXPECT_EQ(scene.object_count(), 1);
}

TEST(CommandStackTest, DepthLimit) {
    Scene scene(800, 600);
    CommandStack stack(5);

    for (int i = 0; i < 10; ++i) {
        auto s = std::make_unique<FreehandStroke>();
        s->add_point({float(i), 0});
        stack.push(scene, std::make_unique<StrokeDrawCmd>(std::move(s)));
    }

    EXPECT_EQ(stack.undo_count(), 5);
}
