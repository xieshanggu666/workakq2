/* 视图：关卡游戏主界面（太阳系画布 + 动作编排 + HUD + 最佳成绩回放）
   挑战模式（challenge 非空）：走社区挑战 API，提交后进入审核队列，
   排行榜/回放来自审核通过的成绩。 */
window.GameView = {
  name: "GameView",
  props: {
    level: Object,
    bodies: Array,
    scores: Object,
    challenge: { type: Object, default: null },
  },
  data() {
    return {
      actions: [],
      uid: 1,
      preview: null,
      result: null,
      running: false,
      hintShown: false,
      expanded: -1,
      replayRecord: null,
      replayLoading: false,
      playerName: Store.get("pilot") || "",
      board: null,
      boardLoading: false,
      mySubs: [],
      timeline: null,
      anim: { playing: false, time: 0, speed: 30 },
      view: { scale: 90, cx: 0, cy: 0 },
      dragging: false,
      lastX: 0, lastY: 0,
      dpr: 1,
    };
  },
  computed: {
    bodyById() {
      const m = {};
      for (const b of this.bodies) m[b.id] = b;
      return m;
    },
    // 回放模式下画布数据来自成绩记录，否则来自实时预览
    activeSrc() { return this.replayRecord || this.preview; },
    traj() { return this.activeSrc ? this.activeSrc.trajectory : null; },
    events() { return this.activeSrc ? this.activeSrc.events : null; },
    simTime() { return this.activeSrc ? this.activeSrc.elapsed_days : 1; },
    bestRecordId() {
      if (this.challenge) return null;  // 挑战模式用排行榜回放，不走内置最佳记录
      const s = this.scores && this.scores[this.level.id];
      return (s && s.record_id) || null;
    },
    fuelRatio() {
      if (!this.activeSrc) return 0;
      return Math.min(1, this.activeSrc.fuel_used / this.level.budget_dv);
    },
    fuelText() {
      if (!this.activeSrc) return "";
      return `${(this.activeSrc.fuel_used * 1731.5).toFixed(1)} / ${(this.level.budget_dv * 1731.5).toFixed(1)} km/s`;
    },
    starBest() {
      if (this.challenge) {
        return (this.challenge.best && this.challenge.best.stars) || 0;
      }
      return (this.scores && this.scores[this.level.id] && this.scores[this.level.id].stars) || 0;
    },
    defaultScale() {
      if (this.challenge) return 55;
      return this.level.id <= 2 ? 90 : this.level.id === 3 ? 70 : 55;
    },
    replayTag() {
      return this.challenge ? "▶ 回放排行榜成绩" : "▶ 回放最佳成绩";
    },
    msText() {
      if (!this.activeSrc) return "";
      const done = (this.activeSrc.milestones || []).map(m => m.name);
      const all = this.level.milestones.map(m => m.name);
      return all.map(n => (done.includes(n) ? "✅" : "⬜") + " " + n).join("　");
    },
    preOk() {
      if (this.replayRecord) {
        return this.replayRecord.ok ? "历史最佳 · 任务达成" : "历史记录 · " + (this.replayRecord.reason || "");
      }
      if (!this.preview) return null;
      if (this.preview.ok) return "任务达成！";
      return "未达成：" + this.preview.reason;
    },
    preOkClass() {
      const ok = this.replayRecord ? this.replayRecord.ok : (this.preview && this.preview.ok);
      return ok ? "ok" : "";
    },
    replayDate() {
      if (!this.replayRecord || !this.replayRecord.created_at) return "";
      const d = new Date(this.replayRecord.created_at * 1000);
      const p = n => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    },
  },
  watch: {
    actions: {
      deep: true,
      handler() {
        if (this.replayRecord) this.replayRecord = null;  // 编辑动作即退出回放
        this._schedulePreview();
      },
    },
  },
  mounted() {
    this._setupCanvas();
    window.addEventListener("resize", this._resize);
    this._raf();
  },
  beforeUnmount() {
    window.removeEventListener("resize", this._resize);
    if (this._rafId) cancelAnimationFrame(this._rafId);
    if (this._previewTimer) clearTimeout(this._previewTimer);
  },
  methods: {
    // ---- 动作编排 ----
    addAction(type) {
      const a = { uid: this.uid++, type };
      if (type === "coast") { a.days = 200; }
      if (type === "burn") { a.angle = 90; a.dv = 0.001; }
      if (type === "slingshot") { a.planet_id = "jupiter"; a.b = 0.01; }
      this.actions.push(a);
      this.expanded = this.actions.length - 1;
      this._preview();
    },
    removeAction(i) {
      this.actions.splice(i, 1);
      if (this.expanded >= this.actions.length) this.expanded = this.actions.length - 1;
      this._preview();
    },
    moveAction(i, dir) {
      const j = i + dir;
      if (j < 0 || j >= this.actions.length) return;
      const tmp = this.actions[i];
      this.actions[i] = this.actions[j];
      this.actions[j] = tmp;
      this.expanded = j;
      this._preview();
    },
    toggleExpand(i) {
      this.expanded = this.expanded === i ? -1 : i;
    },
    actTitle(a) {
      if (a.type === "coast") return `⏳ 等待 ${Math.round(a.days)} 天`;
      if (a.type === "burn") {
        return `🔥 点火 ${(a.dv * 1731.5).toFixed(1)} km/s @ ${Math.round(a.angle)}°`;
      }
      const pl = this.bodyById[a.planet_id];
      return `✦ ${pl ? pl.name : a.planet_id} 弹弓 b=${a.b.toFixed(4)} AU`;
    },
    kmps(v) { return (v * 1731.5).toFixed(1); },
    reset() {
      this.actions = [];
      this.preview = null;
      this.result = null;
      this.replayRecord = null;
      this.anim.time = 0;
      this._preview();
    },
    toggleHint() { this.hintShown = !this.hintShown; },

    // ---- 模拟 ----
    _schedulePreview() {
      if (this._previewTimer) clearTimeout(this._previewTimer);
      this._previewTimer = setTimeout(() => this._preview(), 250);
    },
    async _preview() {
      if (!this.level) return;
      try {
        this.preview = this.challenge
          ? await API.challengePreview(this.challenge.id, this.actions)
          : await API.preview(this.level.id, this.actions);
        if (!this.anim.playing) this.anim.time = Math.min(this.anim.time, this.preview.elapsed_days);
      } catch (e) {
        this.preview = null;
      }
    },
    async launch() {
      if (this.running) return;
      this.running = true;
      // 幂等键：本次发射的成绩提交去重（双击/网络重试不会重复落库）
      const submissionId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      try {
        if (this.challenge) {
          // 挑战模式：服务端结算 → 幂等提交 → 进入审核队列（通过后上榜/回放/解锁）
          const r = await API.challengeRun(this.challenge.id, this.actions);
          this.result = r;
          this.preview = r;
          this.replayRecord = null;
          if (r.ok) {
            Store.set("pilot", this.playerName);
            const sub = await API.challengeSubmit(
              this.challenge.id, r.run_id, submissionId, this.playerName);
            this.result = { ...r, review_status: sub.review_status, record_id: sub.record_id };
            this.$emit("challenge-change");
          }
          this.anim.time = 0;
          this.anim.playing = false;
          return;
        }
        const r = await API.run(this.level.id, this.actions);
        this.result = r;
        this.preview = r;
        this.replayRecord = null;
        if (r.ok) {
          // 成绩关联本次执行档案（run_id）落库，可溯源、可回放
          await API.saveScore(this.level.id, r.run_id, submissionId);
          // 拉取权威最佳成绩（含可回放记录 id），联动星级与关卡解锁
          const sys = await API.system();
          const best = (sys.scores && sys.scores[this.level.id]) || { stars: r.stars };
          this.$emit("score", { level_id: this.level.id, ...best });
        }
        this.anim.time = 0;
        this.anim.playing = false;
      } catch (e) {
        alert("执行失败：" + e.message);
      } finally {
        this.running = false;
      }
    },
    // ---- 挑战排行榜与回放 ----
    async openBoard() {
      if (!this.challenge || this.boardLoading) return;
      this.boardLoading = true;
      try {
        this.board = await API.challengeLeaderboard(this.challenge.id);
        if (this.playerName) {
          this.mySubs = (await API.mySubmissions(this.playerName, this.challenge.id)).submissions || [];
        }
      } catch (e) {
        alert("加载排行榜失败：" + e.message);
      } finally {
        this.boardLoading = false;
      }
    },
    // ---- 申诉与审核链路 ----
    async appealRecord(rec) {
      const reason = window.prompt(
        `对成绩 #${rec.record_id} 的${
          rec.review_status === "revoked" ? "撤销" : "驳回"}发起申诉（第 ${rec.appeal_rounds + 1} 轮，最多 2 轮）：\n复核员会重新核查本次飞行的服务端结算档案。`,
        "");
      if (reason === null) return;
      if (!reason.trim()) { alert("申诉理由不能为空"); return; }
      const appealId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : `ap-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      try {
        await API.appealSubmission(rec.record_id, this.playerName,
                                   reason.trim(), appealId);
        await this.openBoard();
        this.$emit("challenge-change");
        alert("申诉已受理，成绩进入复核队列，请等待复核员裁决。");
      } catch (e) {
        alert("申诉失败：" + e.message);
      }
    },
    async showTimeline(recordId) {
      try {
        this.timeline = await API.submissionTimeline(recordId);
      } catch (e) {
        alert("加载审核链路失败：" + e.message);
      }
    },
    statusLabel(s) {
      return { pending: "待审核", approved: "已上榜", rejected: "已驳回",
               revoked: "已撤销" }[s] || s;
    },
    fmtDateTime(ts) {
      if (!ts) return "";
      const d = new Date(ts * 1000);
      const p = n => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    },
    tlEventLabel(k) {
      return {
        submit: "玩家提交", review_approve: "初审通过", review_reject: "初审驳回",
        appeal: "玩家申诉", appeal_uphold: "复核维持原判",
        appeal_overturn: "复核推翻·改判通过", revoke: "复核撤销上榜",
        restore: "复核恢复上榜", legacy: "历史已审核（补登）",
      }[k] || k;
    },
    tlDetail(e) {
      const d = e.detail || {};
      if (e.kind === "appeal") return `第 ${d.round} 轮：${d.reason || ""}`;
      if (e.kind === "appeal_uphold" || e.kind === "appeal_overturn")
        return `第 ${d.round} 轮${d.note ? "：" + d.note : ""}`;
      return d.note || "";
    },
    async playEntry(e) {
      try {
        const rec = await API.challengeSubmission(e.record_id);
        if (!rec.replayable) {
          alert("该成绩尚未通过审核，暂不能回放。");
          return;
        }
        this.board = null;
        this.replayRecord = rec;
        this.result = null;
        this.anim.time = 0;
        this.anim.playing = true;
      } catch (err) {
        alert("加载回放失败：" + err.message);
      }
    },
    nextLevel() {
      this.$emit("enter", this.level.id + 1);
    },
    // ---- 最佳成绩回放 ----
    async playBest() {
      if (!this.bestRecordId || this.replayLoading) return;
      this.replayLoading = true;
      try {
        const rec = await API.record(this.bestRecordId);
        if (!rec.replayable) {
          alert("该最佳成绩来自旧版存档，没有可回放的轨迹。");
          return;
        }
        this.replayRecord = rec;
        this.result = null;
        this.anim.time = 0;
        this.anim.playing = true;
      } catch (e) {
        alert("加载回放失败：" + e.message);
      } finally {
        this.replayLoading = false;
      }
    },
    exitReplay() {
      this.replayRecord = null;
      this.anim.time = 0;
      this.anim.playing = false;
    },
    loadReplayPlan() {
      // 把最佳记录的动作方案载入编排器，可在此基础上继续调优
      if (!this.replayRecord) return;
      const acts = (this.replayRecord.actions || []).map(a => ({ ...a, uid: this.uid++ }));
      this.replayRecord = null;
      this.actions = acts;
      this.expanded = -1;
      this.anim.time = 0;
      this.anim.playing = false;
    },
    // ---- 动画与画布 ----
    _setupCanvas() {
      this.canvas = this.$refs.cv;
      this.ctx = this.canvas.getContext("2d");
      this.dpr = window.devicePixelRatio || 1;
      this._resize();
      this.view.scale = this.defaultScale;
      // 缩放/平移交互
      this.canvas.addEventListener("wheel", e => {
        e.preventDefault();
        const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
        this.view.scale = Math.max(8, Math.min(400, this.view.scale * f));
      });
      this.canvas.addEventListener("mousedown", e => {
        this.dragging = true; this.lastX = e.clientX; this.lastY = e.clientY;
      });
      window.addEventListener("mousemove", e => {
        if (!this.dragging) return;
        this.view.cx += (e.clientX - this.lastX); this.view.cy += (e.clientY - this.lastY);
        this.lastX = e.clientX; this.lastY = e.clientY;
      });
      window.addEventListener("mouseup", () => { this.dragging = false; });
    },
    _resize() {
      if (!this.canvas) return;
      const r = this.canvas.getBoundingClientRect();
      this.canvas.width = r.width * this.dpr;
      this.canvas.height = r.height * this.dpr;
      this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    },
    _raf() {
      this._rafId = requestAnimationFrame(this._raf);
      if (!this.ctx || !this.canvas) return;
      const w = this.canvas.getBoundingClientRect().width;
      const h = this.canvas.getBoundingClientRect().height;
      this.ctx.clearRect(0, 0, w, h);
      // 平移时把鼠标拖动算进中心
      const cx = this.view.cx, cy = this.view.cy;
      const centerX = w / 2 + cx, centerY = h / 2 + cy;
      System.draw(this.ctx, w, h, {
        bodies: this.bodies,
        view: { scale: this.view.scale, cx: centerX, cy: centerY },
        time: this.anim.time,
        traj: this.traj,
        events: this.events,
        level: this.level,
        probeGlow: this.anim.playing ? 6 : 4.5,
      });
      // 动画推进
      if (this.anim.playing) {
        this.anim.time += this.anim.speed * (1 / 60);
        if (this.anim.time >= this.simTime) {
          this.anim.time = this.simTime;
          this.anim.playing = false;
        }
      }
    },
    play() { if (this.preview || this.replayRecord) { this.anim.playing = !this.anim.playing; } },
    zoom(f) { this.view.scale = Math.max(8, Math.min(400, this.view.scale * f)); },
    resetView() { this.view.scale = this.defaultScale; this.view.cx = 0; this.view.cy = 0; },
  },
  template: `
  <div class="screen game-screen">
    <!-- 顶部 HUD -->
    <header class="hud">
      <button class="btn ghost" @click="$emit('back')">{{ challenge ? '← 挑战列表' : '← 关卡' }}</button>
      <div class="hud-title">
        <h2>
          {{ challenge ? '挑战 #' + challenge.id + ' · ' + level.name : '第 ' + level.id + ' 关 · ' + level.name }}
          <span v-if="challenge" class="ver-tag">v{{ challenge.current_version }}</span>
        </h2>
        <span class="best-stars">
          最佳
          <template v-for="i in 3" :key="i">
            <span :class="i <= starBest ? 'lit' : 'dim'">★</span>
          </template>
        </span>
      </div>
      <div class="hud-fuel">
        <div class="fuel-label">燃料 {{ fuelText }}</div>
        <div class="fuel-bar"><div class="fuel-fill" :style="{width: (fuelRatio*100)+'%'}"></div></div>
      </div>
      <button v-if="challenge" class="btn ghost" :disabled="boardLoading" @click="openBoard">
        {{ boardLoading ? '加载中…' : '🏅 排行榜' }}
      </button>
      <button v-if="bestRecordId && !replayRecord" class="btn ghost" :disabled="replayLoading" @click="playBest">
        {{ replayLoading ? '加载中…' : '🏆 最佳回放' }}
      </button>
      <button class="btn ghost" @click="toggleHint">{{ hintShown ? '隐藏提示' : '策略提示' }}</button>
      <button class="btn ghost" @click="reset">重置</button>
      <button class="btn primary" :disabled="running" @click="launch">
        {{ running ? '计算中…' : '🚀 发射' }}
      </button>
    </header>

    <div class="game-body">
      <!-- 画布 -->
      <div class="canvas-wrap" ref="wrap">
        <canvas ref="cv"></canvas>
        <div class="canvas-hud">
          <div class="chud-row">
            <span>⏱ {{ Math.round(anim.time) }} / {{ Math.round(simTime) }} 天</span>
            <span class="chud-btns">
              <button class="btn mini" @click="play">{{ anim.playing ? '⏸' : '▶' }}</button>
              <button class="btn mini" @click="zoom(1.3)">+</button>
              <button class="btn mini" @click="zoom(1/1.3)">−</button>
              <button class="btn mini" @click="resetView">⛶</button>
            </span>
          </div>
          <div class="chud-ms">{{ msText }}</div>
          <div class="chud-status" :class="preOkClass">{{ preOk }}</div>
        </div>
        <div v-if="replayRecord" class="replay-bar">
          <span class="replay-tag">{{ replayTag }}</span>
          <span class="replay-meta">
            <template v-for="i in 3" :key="i"><span :class="i <= replayRecord.stars ? 'lit' : 'dim'">★</span></template>
            · {{ (replayRecord.fuel_used * 1731.5).toFixed(1) }} km/s
            · {{ Math.round(replayRecord.elapsed_days) }} 天
            · {{ replayDate }}
          </span>
          <button class="btn mini" @click="loadReplayPlan">载入方案</button>
          <button class="btn mini" @click="exitReplay">退出回放</button>
        </div>
        <div class="canvas-tip">滚轮缩放 · 拖动平移 · ▶ 播放轨迹回放</div>
      </div>

      <!-- 动作编排面板 -->
      <aside class="planner">
        <h3>任务计划 <span class="plan-count">{{ actions.length }} 步</span></h3>
        <div v-if="challenge" class="player-row">
          <label>飞行员署名</label>
          <input v-model.trim="playerName" maxlength="24" placeholder="匿名飞行员">
        </div>
        <div class="add-row">
          <button class="btn chip" @click="addAction('coast')">+ 等待</button>
          <button class="btn chip" @click="addAction('burn')">+ 点火</button>
          <button class="btn chip" @click="addAction('slingshot')">+ 弹弓</button>
        </div>

        <div class="plan-list" v-if="actions.length">
          <div v-for="(a, i) in actions" :key="a.uid" class="plan-row"
               :class="{ open: expanded === i }">
            <div class="plan-head" @click="toggleExpand(i)">
              <span class="step-idx">{{ i + 1 }}</span>
              <span class="step-title">{{ actTitle(a) }}</span>
              <span class="step-ops">
                <button class="icon-btn" @click.stop="moveAction(i, -1)" :disabled="i === 0">↑</button>
                <button class="icon-btn" @click.stop="moveAction(i, 1)" :disabled="i === actions.length - 1">↓</button>
                <button class="icon-btn danger" @click.stop="removeAction(i)">✕</button>
              </span>
            </div>
            <div v-if="expanded === i" class="plan-edit">
              <!-- 等待 -->
              <template v-if="a.type === 'coast'">
                <label>等待 <b>{{ Math.round(a.days) }}</b> 天（后续动作最早此刻执行）</label>
                <input type="range" min="10" max="4000" step="5" v-model.number="a.days">
              </template>
              <!-- 点火 -->
              <template v-if="a.type === 'burn'">
                <label>方向 <b>{{ Math.round(a.angle) }}°</b>（0°=右向，逆时针）</label>
                <input type="range" min="-180" max="180" step="1" v-model.number="a.angle">
                <label>ΔV <b>{{ kmps(a.dv) }} km/s</b>（{{ a.dv.toFixed(4) }} AU/天）</label>
                <input type="range" min="0" max="0.004" step="0.00005" v-model.number="a.dv">
              </template>
              <!-- 弹弓 -->
              <template v-if="a.type === 'slingshot'">
                <label>目标行星</label>
                <div class="planet-pick">
                  <button v-for="pid in ['venus','earth','mars','jupiter','saturn']" :key="pid"
                          class="planet-chip" :class="{ sel: a.planet_id === pid }"
                          :style="{ '--pc': (bodyById[pid] || {}).color }"
                          @click="a.planet_id = pid">{{ (bodyById[pid] || {}).name }}</button>
                </div>
                <label>近心点 <b>{{ a.b.toFixed(4) }} AU</b>（负号 = 换一侧绕行）</label>
                <input type="range" min="-0.15" max="0.15" step="0.0005" v-model.number="a.b">
                <p class="plan-note">弹弓会在探测器进入该行星影响球时自动结算，越紧甩得越狠。</p>
              </template>
            </div>
          </div>
        </div>
        <p v-else class="plan-empty">还没有动作。先加一次点火，把探测器推出地球轨道。</p>

        <div v-if="hintShown" class="hint-box">💡 {{ level.hint }}</div>
      </aside>
    </div>

    <!-- 结算弹窗 -->
    <div v-if="result" class="modal-mask" @click.self="result = null">
      <div class="modal" :class="result.ok ? 'ok' : 'fail'">
        <div class="modal-stars">
          <template v-for="i in 3" :key="i">
            <span :class="i <= result.stars ? 'lit big' : 'dim big'">★</span>
          </template>
        </div>
        <h2>{{ result.ok ? '任务达成' : '任务失败' }}</h2>
        <p class="modal-reason">{{ result.reason }}</p>
        <div class="modal-stats">
          <div><b>{{ (result.fuel_used * 1731.5).toFixed(1) }}</b> km/s 燃料</div>
          <div><b>{{ result.elapsed_days.toFixed(0) }}</b> 天</div>
          <div><b>{{ (result.milestones || []).length }}/{{ level.milestones.length }}</b> 里程碑</div>
        </div>
        <div class="modal-ms">{{ msText }}</div>
        <p v-if="challenge && result.ok && result.review_status" class="review-note">
          📋 飞行记录已提交审核（{{ result.review_status === 'pending' ? '待审核' : result.review_status }}），
          通过后进入排行榜、开放回放并联动关卡解锁。
        </p>
        <div class="modal-btns">
          <button class="btn ghost" @click="result = null; anim.playing = true">▶ 回放</button>
          <button class="btn ghost" @click="result = null; reset()">重玩</button>
          <button v-if="!challenge && result.ok && level.id < 5" class="btn primary" @click="nextLevel">下一关 →</button>
          <button v-if="challenge && result.ok" class="btn primary" @click="result = null; openBoard()">🏅 排行榜</button>
          <button v-if="!result.ok" class="btn primary" @click="result = null">继续调整</button>
        </div>
      </div>
    </div>

    <!-- 挑战排行榜弹窗 -->
    <div v-if="board" class="modal-mask" @click.self="board = null">
      <div class="modal board-modal">
        <h2>🏅 排行榜 <span class="ver-tag">v{{ board.version }}</span></h2>
        <p class="modal-reason">审核通过的成绩 · 每名玩家取最佳一条</p>
        <div v-if="board.entries && board.entries.length" class="board-list">
          <div v-for="e in board.entries" :key="e.record_id" class="board-row">
            <span class="board-rank" :class="'r' + e.rank">{{ e.rank }}</span>
            <span class="board-player">{{ e.player }}</span>
            <span class="board-stars">
              <template v-for="i in 3" :key="i">
                <span :class="i <= e.stars ? 'lit' : 'dim'">★</span>
              </template>
            </span>
            <span class="board-meta">
              {{ (e.fuel_used * 1731.5).toFixed(1) }} km/s · {{ Math.round(e.elapsed_days) }} 天
            </span>
            <button class="btn mini" @click="playEntry(e)">▶ 回放</button>
          </div>
        </div>
        <p v-else class="plan-empty board-empty">
          还没有审核通过的成绩，来当第一个上榜的飞行员！
        </p>

        <!-- 我在本挑战的成绩：驳回/撤销可申诉，全链路可查 -->
        <div v-if="mySubs.length" class="board-mine">
          <h4>🧑‍✈️ 我的提交（{{ mySubs.length }}）</h4>
          <div v-for="m in mySubs" :key="m.record_id" class="mine-row">
            <span class="mine-meta">
              #{{ m.record_id }} · ★{{ m.stars }} · {{ (m.fuel_used * 1731.5).toFixed(1) }} km/s
              · {{ Math.round(m.elapsed_days) }} 天
            </span>
            <span class="mine-status" :class="'s-' + m.review_status">
              {{ statusLabel(m.review_status) }}
              <template v-if="m.appeal_open"> · 第 {{ m.appeal_rounds }} 轮申诉复核中</template>
            </span>
            <span v-if="m.review_note" class="mine-note">备注：{{ m.review_note }}</span>
            <button class="btn mini" :disabled="!m.appeal_available"
                    @click="appealRecord(m)">
              {{ m.appeal_rounds >= 2 ? '申诉次数已用尽'
                 : (m.appeal_open ? '申诉复核中' : '⚖ 申诉（剩 ' + (2 - m.appeal_rounds) + ' 轮）') }}
            </button>
            <button class="btn mini ghost" @click="showTimeline(m.record_id)">链路</button>
          </div>
        </div>
        <div class="modal-btns">
          <button class="btn ghost" @click="board = null">关闭</button>
        </div>
      </div>
    </div>

    <!-- 审核链路时间线 -->
    <div v-if="timeline" class="modal-mask" @click.self="timeline = null">
      <div class="modal timeline-modal">
        <h2>🔗 审核链路 · 成绩 #{{ timeline.record_id }}</h2>
        <p class="modal-reason">
          #{{ timeline.challenge_id }} {{ timeline.challenge_title }} · v{{ timeline.version }}
          · {{ timeline.player }} · ★{{ timeline.stars }}
          · <span :class="'s-' + timeline.review_status">{{ statusLabel(timeline.review_status) }}</span>
        </p>
        <ul class="timeline-list">
          <li v-for="e in timeline.events" :key="e.seq" class="tl-item">
            <div class="tl-dot" :class="'dot-' + e.actor_role"></div>
            <div class="tl-body">
              <div class="tl-head">
                <b>{{ tlEventLabel(e.kind) }}</b>
                <span class="tl-actor">{{ e.actor || '—' }}（{{
                  { player: '玩家', reviewer: '初审员', moderator: '复核员',
                    system: '系统' }[e.actor_role] || e.actor_role }}）</span>
                <span class="tl-time">{{ fmtDateTime(e.created_at) }}</span>
              </div>
              <div v-if="tlDetail(e)" class="tl-detail">{{ tlDetail(e) }}</div>
            </div>
          </li>
        </ul>
        <div v-if="timeline.appeals && timeline.appeals.length" class="tl-appeals">
          <div v-for="a in timeline.appeals" :key="a.appeal_id" class="tl-appeal">
            第 {{ a.round }} 轮：{{ a.reason }}
            → <b :class="a.status === 'overturn' ? 'ok-text' : 'bad-text'">
              {{ a.status === 'overturn' ? '推翻原判' : '维持原判' }}</b>
            <span v-if="a.decided_by">（{{ a.decided_by }}：{{ a.decision_note }}）</span>
          </div>
        </div>
        <div class="modal-btns">
          <button class="btn ghost" @click="timeline = null">关闭</button>
        </div>
      </div>
    </div>
  </div>`,
};
