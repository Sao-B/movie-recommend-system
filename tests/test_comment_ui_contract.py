"""详情页「评分 ↔ 影评」的界面契约。

这组约定前后改过几轮，这里把当前口径锁住，避免以后重构时又退回去：

1. 详情页不会「打 0 分」：el-rate 不开 ``clearable``（开了就会「点同一颗星＝0 分」，
   双击尤其容易误触）。星星旁**也不再给单独的「删除」**——撤销评分的入口只剩影评
   卡片上那一个（见第 8 条），免得出现「有影评、没评分」这种半截状态没人收拾。
2. **评分与影评必须同时存在才能发表，缺一不可**：只有评分没写正文、或只写正文没打分，
   「发表」都点不动，并在下面说清还差哪一半。提交顺序固定为「先评分、再影评」
   （服务端同样要求此刻有评分，见 ``create_comment``）。录入区里星星在评论框**上面**、
   用户名在星星**左边**。
3. **发表之后没有「修改」按钮**：输入框那个位置被用户自己的影评顶上去（置顶展示），
   表单不会以任何形式回来。改正文与改评分**都只走卡片上的「编辑」**——展示态的星星
   一律只读，进了编辑态才能点，且要**点「保存」才落库**（不即时改）。
4. **全页同时只许一条影评在编辑**：置顶那条与时间线里自己那条是同一份数据，两处以及
   时间线上别人的影评之间都要互斥。编辑态由 ``CommentSection`` 用 ``editingKey``
   统一持有（key 形如 ``pinned:<id>`` / ``list:<id>``），卡片自己不存；别处正在编辑时，
   其余卡片的「编辑」按钮禁用。
5. **时间只在卡片底部显示一处**：「发布于 ``created_at``」（发表时间，永不改）
   + 改过正文才出现的「上次修改于 ``edited_at``」。右上角那个时间已经撤掉。
6. **评分统计里显示评分人数**：``rating-stats`` 的 ``count`` 是 MovieLens 评分用户数
   与本站 ``user_ratings`` 的合计（后端已合并，页面上不区分来源）。
7. **置顶那条与时间线之间有一条分割线**。
8. **删除只有一个入口**：影评卡片上的「删除」（置顶那条与时间线里自己那条是同一个
   组件）。确认框问「影评草稿要不要保留」——保留＝只删评分（``DELETE /api/ratings/<id>``），
   影评退成「仅自己可见」并明确告知；不保留＝两行一起删
   （``DELETE /api/comments/<id>?with_rating=1``），置顶那块随即变回输入框。
9. 改了评分或影评却没点提交就离开详情页，要弹确认框提醒。
"""
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "frontend" / "src"
COMPONENTS = SRC / "components"
VIEWS = SRC / "views"


class DetailRatingUiContractTests(unittest.TestCase):
    def _read(self, name):
        return (COMPONENTS / name).read_text(encoding="utf-8")

    def _read_view(self, name):
        return (VIEWS / name).read_text(encoding="utf-8")

    def _template(self, name):
        """只看 <template>：注释里要能自由解释这些坑为什么不能踩。"""
        return self._read(name).rsplit("</template>", 1)[0]

    def _editable_block(self):
        """CommentSection 里「还没发表」那一支（输入框 + 星星那一行）。"""
        template = self._template("CommentSection.vue")
        return template.split('v-if="!hasPublished"', 1)[1].split("<template v-else>", 1)[0]

    def _published_block(self):
        """CommentSection 里「已发表」那一支（置顶卡片 + 分割线）。"""
        template = self._template("CommentSection.vue")
        return template.split("<template v-else>", 1)[1].split("</template>", 1)[0]

    # ---------- 1. 详情页不会「打 0 分」，也没有「只删评分」的星星删除 ----------

    def test_rate_control_is_not_clearable(self):
        """el-rate 不开 clearable，否则双击已选中的星星会清成 0 分。

        编辑态里那颗可点的星星同理：清成 0 既不是合法评分，也会把「评分与影评一体」
        的约定撕开一个口子。

        只看模板：脚本注释里解释「为什么不开 clearable」是允许的，不该被这条误伤。
        """
        self.assertNotIn("clearable", self._template("CommentSection.vue"))
        self.assertNotIn("clearable", self._template("CommentItem.vue"))

    def test_section_no_longer_explains_click_to_clear(self):
        """那句「点击已选中的星星即可清零」的解释文案已经撤掉。"""
        self.assertNotIn("点击已选中的星星", self._read("CommentSection.vue"))

    def test_star_delete_link_is_gone(self):
        """星星旁的「删除」整个撤掉：撤销评分只走影评卡片上的确认框。"""
        section = self._read("CommentSection.vue")
        self.assertNotIn("rate-delete", section)
        self.assertNotIn("removeRating", section)
        self.assertNotIn("canRemoveRating", section)
        # 连接口也不该在详情页的评分控件这一侧被调到
        self.assertNotIn("apiRatings.remove", section)
        # 旧的「未评分影评仅自己可见」提示同理作废（那条路现在从删除走）
        self.assertNotIn("unratedHint", section)
        self.assertNotIn("emit('update:my-rating', 0)", section)
        self.assertNotIn("apiRatings.submit(props.movieId, 0)", section)

    # ---------- 2. 评分与影评缺一不可，且星星在评论框上面 ----------

    def test_publish_requires_both_rating_and_comment(self):
        section = self._read("CommentSection.vue")
        self.assertIn(
            "() => props.myRating > 0 && !!newComment.value.trim()", section
        )
        self.assertIn(':disabled="!canSubmit"', section)

    def test_hint_says_which_half_is_missing(self):
        section = self._read("CommentSection.vue")
        self.assertIn("还差一句影评：评分和影评需要同时填写才能发表。", section)
        self.assertIn("还差一个评分：评分和影评需要同时填写才能发表。", section)

    def test_rating_is_submitted_before_the_comment(self):
        """顺序不能反：服务端要求「有评分的影评」才算成对发表。"""
        section = self._read("CommentSection.vue")
        self.assertLess(
            section.index("await apiRatings.submit(props.movieId, props.myRating)"),
            section.index("await apiComments.create(props.movieId, content)"),
        )

    def test_rate_row_sits_above_the_textarea_with_the_username(self):
        """录入区：星星在评论框上面，用户名紧挨在星星左边。"""
        editable = self._editable_block()
        self.assertIn('<div class="rate-row">', editable)
        self.assertIn("{{ auth.user?.username }}", editable)
        # 星星那一行整块压在 textarea 上方
        self.assertLess(
            editable.index('class="rate-row"'), editable.index('v-model="newComment"')
        )
        # 用户名在 el-rate 左边
        self.assertLess(
            editable.index("{{ auth.user?.username }}"), editable.index("<el-rate")
        )
        # 上一版那个「星星与按钮同一行」的排版已经作废
        self.assertNotIn("rate-block", editable)

    # ---------- 3. 发表之后：输入框换成自己的影评；改评分要走「编辑 + 保存」 ----------

    def test_published_comment_takes_the_input_box_place(self):
        section = self._read("CommentSection.vue")
        self.assertIn("const hasPublished = computed(() => !!myComment.value)", section)

        editable = self._editable_block()
        published = self._published_block()
        # 可编辑态＝输入框；已发表态＝自己的影评（置顶卡片），输入框不再出现
        self.assertIn('v-model="newComment"', editable)
        self.assertNotIn('v-model="newComment"', published)
        self.assertIn(':comment="myComment"', published)
        self.assertIn("pinned", published)
        self.assertNotIn("pinned", editable)

    def test_published_state_has_no_modify_button(self):
        """发表之后不再有「修改」按钮，也没有能把表单调回来的开关。"""
        section = self._read("CommentSection.vue")
        self.assertNotIn("published-actions", section)
        self.assertNotIn("startModify", section)
        self.assertNotIn("cancelModify", section)
        # 表单区不再自己持有「编辑中」这个开关（编辑态已移到卡片上、由 editingKey 管）
        self.assertNotIn("const editing = ref(false)", section)
        # 发表按钮只剩「发表」一个文案
        label = section.split('@click="submitAll"', 1)[1].split("</el-button>", 1)[0]
        self.assertIn("发表", label)
        self.assertNotIn("修改", label)

    def test_publish_only_creates_never_updates_from_the_section(self):
        """表单只会在「还没发表」时展开，所以详情页这一侧只剩新建（POST）。

        改正文与改评分是卡片上那一个入口的事（见下面几条），不在这里再走一遍 PUT。
        """
        section = self._read("CommentSection.vue")
        self.assertIn("await apiComments.create(props.movieId, content)", section)
        self.assertNotIn("apiComments.update", section)
        self.assertNotIn("isModify", section)

    def test_display_star_is_read_only_for_everyone(self):
        """展示态（没点「编辑」）时，谁都不许直接点星星改评分。"""
        item = self._read("CommentItem.vue")
        template = self._template("CommentItem.vue")
        self.assertNotIn(':disabled="!isMine"', template)
        display_rate = template.split("<el-rate", 1)[1].split("/>", 1)[0]
        self.assertIn("disabled", display_rate)
        self.assertIn(':model-value="comment.user_rating"', display_rate)
        # 上一版那条「点一下就落库」的即时改链路已经撤掉
        self.assertNotIn("onRateEdit", item)
        self.assertNotIn("rating-edited", item)

    def test_rating_is_editable_only_inside_the_edit_area(self):
        """编辑区里才给一颗可点的星星，值先落在草稿 draftRating 上。"""
        template = self._template("CommentItem.vue")
        self.assertIn('class="edit-rate-row"', template)
        edit_rate = template.split('class="edit-rate-row"', 1)[1]
        edit_rate = edit_rate.split("<el-rate", 1)[1].split("/>", 1)[0]
        self.assertIn(':model-value="draftRating"', edit_rate)
        self.assertIn('@update:model-value="draftRating = $event"', edit_rate)
        self.assertNotIn("disabled", edit_rate)

    def test_rating_and_text_are_committed_together_on_save(self):
        """点「保存」才落库：先评分、再影评；两者都没变就当作取消，不白刷 edited_at。"""
        item = self._read("CommentItem.vue")
        save = item.split("async function saveEdit()", 1)[1].split("\n}", 1)[0]
        self.assertIn(
            "await apiRatings.submit(props.comment.movie_id, Number(draftRating.value))",
            save,
        )
        self.assertIn("await apiComments.update(props.comment.id, content)", save)
        self.assertLess(save.index("apiRatings.submit"), save.index("apiComments.update"))
        self.assertIn("emit('saved', props.editKey, ratingChanged)", save)
        self.assertIn("emit('cancel-edit')", save)
        # 0 不是合法评分：草稿是 0 时不算「改了评分」（判定在 draftHasChange 里）
        self.assertIn(
            "const ratingChanged = rating > 0 && rating !== (props.comment.user_rating ?? 0)",
            item,
        )

    def test_saving_a_new_rating_refreshes_list_and_stats(self):
        """保存成功：先收起编辑态、再刷列表；评分也改了才让详情页重拉统计。"""
        section = self._read("CommentSection.vue")
        self.assertIn('@saved="onSaved"', section)
        handler = section.split("async function onSaved", 1)[1].split("\n}", 1)[0]
        self.assertIn("editingKey.value = ''", handler)
        self.assertIn("await reloadComments()", handler)
        self.assertIn("if (ratingChanged) emit('rating-submitted')", handler)

    # ---------- 4. 编辑互斥：全页同时只许一条影评在编辑 ----------

    def test_only_one_comment_can_be_edited_at_a_time(self):
        """编辑态由父组件用 editingKey 持有，卡片自己不存（两条 id 相同也没关系）。"""
        section = self._read("CommentSection.vue")
        item = self._read("CommentItem.vue")
        template = self._template("CommentSection.vue")

        self.assertIn("const editingKey = ref('')", section)
        self.assertIn("function onStartEdit(key)", section)
        self.assertIn("function onCancelEdit()", section)
        # 置顶那条：key 用 computed 算，不在模板里写读 myComment 的闭包
        self.assertIn("const pinnedKey = computed(", section)
        self.assertIn(':edit-key="pinnedKey"', template)
        self.assertIn(':editing="editingKey === pinnedKey"', template)
        # 时间线里每条：位置前缀 + id，与置顶那条天然不同
        self.assertIn(':edit-key="\'list:\' + comment.id"', template)
        self.assertIn(':editing="editingKey === \'list:\' + comment.id"', template)
        # 卡片侧的三个 prop
        self.assertIn("editKey: { type: String, required: true }", item)
        self.assertIn("editing: { type: Boolean, default: false }", item)
        self.assertIn("editLocked: { type: Boolean, default: false }", item)

    def test_other_cards_actions_are_locked_while_editing(self):
        """别处在编辑 → 本卡片的操作全部禁用（不止「编辑」，「删除」也一样）。"""
        template = self._template("CommentSection.vue")
        item = self._read("CommentItem.vue")
        self.assertIn(
            ':edit-locked="!!editingKey && editingKey !== pinnedKey"', template
        )
        self.assertIn(
            ":edit-locked=\"!!editingKey && editingKey !== 'list:' + comment.id\"",
            template,
        )
        item_template = self._template("CommentItem.vue")
        # 编辑、删除两个按钮都挂同一个禁用条件，并共用一句悬停说明
        self.assertEqual(item_template.count(':disabled="editLocked"'), 2)
        self.assertEqual(item_template.count(':title="lockTitle"'), 2)
        self.assertIn("editLocked ? '已有另一条影评正在编辑，请先保存或取消'", item)

    def test_a_locked_card_cannot_be_deleted_even_if_the_click_gets_through(self):
        """光有 disabled 不够：`remove()` 自己也挡一道，防止程序化调用绕过按钮状态。"""
        item = self._read("CommentItem.vue")
        self.assertIn("if (props.editLocked) return", item)
        # 守卫必须排在「取出 movieId / commentId」之前：先挡住，再谈别的
        remove_body = item[item.index("async function remove()"):]
        self.assertLess(
            remove_body.index("if (props.editLocked) return"),
            remove_body.index("const movieId = props.comment.movie_id"),
        )
        # 进入编辑态的入口同样带守卫
        self.assertIn("function onStartEdit() {\n  if (props.editLocked) return", item)

    def test_a_locked_card_is_visually_dimmed(self):
        """被锁住的卡片整体压暗，让用户一眼看出「这条现在动不了」。"""
        template = self._template("CommentItem.vue")
        self.assertIn(':class="{ pinned, locked: editLocked }"', template)
        item = self._read("CommentItem.vue")
        self.assertIn(".comment-item.locked {", item)

    # ---------- 5. 时间只显示一处 / 6. 评分人数 ----------

    def test_time_shows_once_in_the_footer(self):
        """右上角那个发布时间撤掉，底部一处说清「发布 + 最后修改」。"""
        template = self._template("CommentItem.vue")
        self.assertNotIn('<span class="time">', template)
        self.assertIn("发布于 {{ formatTime(comment.created_at) }}", template)
        self.assertIn('v-if="comment.edited_at"', template)
        self.assertIn("上次修改于 {{ formatTime(comment.edited_at) }}", template)
        # 正文段落之后才是时间那行，别再回到卡片顶上
        self.assertLess(
            template.index('class="comment-content"'),
            template.index("发布于 {{ formatTime(comment.created_at) }}"),
        )

    def test_stats_show_the_rating_count_below_the_average(self):
        """评分统计里显示评分人数＝MovieLens 全量 + 本站（后端已合并，不区分来源）。"""
        stats = self._read("RatingStats.vue")
        self.assertIn("评分人数 {{ countText }}", stats)
        self.assertIn("Number(props.count || 0).toLocaleString('zh-CN')", stats)
        # 人数压在平均分星条下方
        self.assertLess(
            stats.index('class="stars"'), stats.index('class="rate-count"')
        )

    # ---------- 7. 置顶与时间线之间的分割线 ----------

    def test_divider_separates_the_pinned_comment_from_the_timeline(self):
        published = self._published_block()
        self.assertIn('<el-divider class="pin-divider" />', published)
        # 分割线在置顶卡片之后
        self.assertLess(
            published.index(':comment="myComment"'),
            published.index('class="pin-divider"'),
        )
        # 未发表那一支不带分割线
        self.assertNotIn("pin-divider", self._editable_block())

    def test_list_refreshes_before_stats_reload(self):
        """先刷影评列表、再让详情页重拉统计。

        星星要等列表回来才变；顺序反过来时详情页统计先跳、评论条星级后跳，
        看起来像分两步完成，容易让人以为第一次没生效。
        """
        section = self._read("CommentSection.vue")
        self.assertLess(
            section.index("await reloadComments()"),
            section.index("emit('rating-submitted')"),
        )

    # ---------- 8. 删除：问「影评草稿要不要保留」 ----------

    def test_delete_asks_whether_to_keep_the_draft(self):
        item = self._read("CommentItem.vue")
        self.assertIn("async function remove", item)
        self.assertIn(
            "const hasRating = computed(() => props.comment.user_rating > 0)", item
        )
        # 有评分：两选一的确认框，两边后果都写清
        self.assertIn("这条影评草稿要保留吗？", item)
        self.assertIn("confirmButtonText: '保留影评'", item)
        self.assertIn("cancelButtonText: '不保留'", item)
        self.assertIn("distinguishCancelAndClose: true", item)
        self.assertIn("影评留在原地，但只对你自己可见（重新评分即自动公开）", item)
        self.assertIn("· 不保留：评分和影评一起删除。", item)
        # 没评分时用简单问法，不必硬提一个不存在的评分
        self.assertIn("确定要删除这条影评吗？", item)

    def test_keeping_the_draft_only_deletes_the_rating(self):
        """保留草稿：走删除评分那个接口，影评留在原地，并把结果告诉用户。"""
        item = self._read("CommentItem.vue")
        self.assertIn("await apiRatings.remove(movieId)", item)
        self.assertIn("ElMessage.success('评分已删除，这条影评现在仅你自己可见')", item)

    def test_not_keeping_the_draft_deletes_both_rows(self):
        """不保留：带上 with_rating 让后端把两行在同一次提交里删掉。"""
        item = self._read("CommentItem.vue")
        self.assertIn("await apiComments.delete(commentId, true)", item)
        self.assertIn("ElMessage.success('影评与评分已删除')", item)

    def test_delete_sees_the_id_and_movie_id_before_awaiting(self):
        """两个 id 先取下来再弹框/发请求，免得中途 props 变了话说一套做一套。"""
        item = self._read("CommentItem.vue")
        self.assertIn("const movieId = props.comment.movie_id", item)
        self.assertIn("const commentId = props.comment.id", item)

    def test_both_delete_paths_report_the_rating_is_gone(self):
        item = self._read("CommentItem.vue")
        # 保留 / 不保留都会让评分消失 → 都要让详情页重拉统计
        self.assertIn("emit('rating-removed')", item)
        # 只有「本来就没评分」那条路只重拉列表（置顶退回输入框）
        self.assertIn("emit('changed')", item)
        self.assertIn(
            "'rating-removed'", item.split("defineEmits", 1)[1].split(")", 1)[0]
        )

    def test_section_wires_the_delete_back_to_the_detail_page(self):
        """报「评分没了」时，CommentSection 要先刷列表、再让详情页重拉统计。"""
        section = self._read("CommentSection.vue")
        self.assertIn('@rating-removed="onRatingRemoved"', section)
        self.assertIn(':movie-title="movieTitle"', section)
        handler = section.split("async function onRatingRemoved", 1)[1].split("}", 1)[0]
        self.assertIn("await reloadComments()", handler)
        self.assertIn("emit('rating-submitted')", handler)

    def test_detail_page_passes_the_title_for_the_confirm(self):
        """确认框要能说清删的是哪一部，所以详情页得把片名传下来。"""
        detail = self._read_view("MovieDetailView.vue")
        self.assertIn(':movie-title="movie.title"', detail)
        self.assertIn("movieTitle", self._read("CommentSection.vue"))

    def test_api_layer_maps_the_flag_to_the_query_param(self):
        """接口层只在 withRating 为 true 时才带 with_rating=1。"""
        api = (SRC / "api" / "comments.js").read_text(encoding="utf-8")
        self.assertIn("withRating = false", api)
        self.assertIn("params: withRating ? { with_rating: 1 } : {}", api)

    # ---------- 9. 未提交修改的离开提醒 ----------

    def test_pending_changes_are_tracked(self):
        section = self._read("CommentSection.vue")
        # 表单展开着（即还没发表）、改了评分或正文，都算「有待提交的改动」
        self.assertIn("hasPendingChange", section)
        self.assertIn("props.myRating !== props.savedRating", section)
        self.assertIn("if (hasPublished.value) return false", section)
        self.assertIn("dirtyKeys", section)
        self.assertIn("@dirty", section)

    def test_leaving_with_pending_changes_asks_first(self):
        section = self._read("CommentSection.vue")
        # 路由跳走：确认＝提交后离开；取消＝不提交直接离开
        self.assertIn("onBeforeRouteLeave", section)
        self.assertIn("ElMessageBox.confirm", section)
        self.assertIn("distinguishCancelAndClose", section)
        # 刷新 / 关标签页也拦一下
        self.assertIn("beforeunload", section)

    def test_no_pending_revoke_state_or_zero_post_left(self):
        """删除不攒到「发表」：没有 ratingRemoved 那一档，0 也永远不会被 POST 出去。"""
        section = self._read("CommentSection.vue")
        self.assertNotIn("ratingRemoved", section)
        # 0 分在开头就被挡掉（按钮也点不动），后面判断「改没改」才不用再提一遍
        self.assertIn("if (props.myRating <= 0 || !content)", section)
        self.assertIn("props.myRating !== props.savedRating", section)

    def test_item_reports_dirty_edits(self):
        item = self._read("CommentItem.vue")
        self.assertIn("emit('dirty'", item)
        self.assertIn("'dirty'", item.split("defineEmits", 1)[1].split(")", 1)[0])

    def test_dirty_callback_carries_its_own_key(self):
        """脏标记的 key 必须由卡片自己报，不能在父模板里写 myComment.id。

        Vue 会把这种内联箭头缓存成闭包、调用时才去读 myComment；卡片因列表刷新被
        卸载时 myComment 已经是 null，取 ``.id`` 直接抛 TypeError，把这一次 DOM 补丁
        整个打断——表现出来就是「列表卡在 loading、卡片不刷新、列表数字却变了」。
        这条是实机调试抓到的（见删影评那条链路），别改回去。

        另外 key 必须是 editKey（位置 + id），不能退化成 comment.id：置顶那条与时间线
        里自己那条 id 相同，用 id 会互相覆盖对方的脏标记。
        """
        section = self._read("CommentSection.vue")
        item = self._read("CommentItem.vue")
        template = self._template("CommentSection.vue")
        self.assertIn('@dirty="onItemDirty"', template)
        self.assertNotIn("onItemDirty(myComment.id", template)
        self.assertNotIn("onItemDirty(comment.id", template)
        self.assertIn("function onItemDirty(key, dirty)", section)
        self.assertIn("emit('dirty', props.editKey", item)
        self.assertIn(
            "onBeforeUnmount(() => emit('dirty', props.editKey, false))", item
        )


if __name__ == "__main__":
    unittest.main()
