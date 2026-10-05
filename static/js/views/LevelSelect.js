/* 视图：关卡选择 */
window.LevelSelect = {
  name: "LevelSelect",
  props: ["system", "scores"],
  methods: {
    starOf(id) { return (this.scores && this.scores[id] && this.scores[id].stars) || 0; },
    unlocked(id) {
      if (id === 1) return true;
      return this.starOf(id - 1) >= 1;
    },
    enter(lv) { this.$emit("enter", lv.id); },
  },
  computed: {
    levels() {
      return (this.system && this.system.levels) || [];
    },
    bodies() {
      return (this.system && this.system.bodies) || [];
    },
    bodyById() {
      const m = {};
      for (const b of this.bodies) m[b.id] = b;
      return m;
    },
    totalStars() {
      let s = 0;
      for (const lv of this.levels) s += this.starOf(lv.id);
      return s;
    },
  },
  template: `
  <div class="screen levels-screen">
    <header class="topbar">
      <div class="logo">
        <div class="logo-mark">✦</div>
        <div>
          <h1>引力跳板</h1>
          <p>星际弹弓轨道规划 · Gravity Slingshot</p>
        </div>
      </div>
      <div class="topbar-right">
        <button class="btn ghost community-btn" @click="$emit('challenges')">🌐 社区航线挑战</button>
        <span class="star-sum" title="总星数">★ {{ totalStars }} / 15</span>
      </div>
    </header>

    <div class="level-grid">
      <div v-for="lv in levels" :key="lv.id"
           class="level-card" :class="{ locked: !unlocked(lv.id) }"
           @click="unlocked(lv.id) && enter(lv)">
        <div class="level-num">第 {{ lv.id }} 关</div>
        <h2>{{ lv.name }}</h2>
        <p class="brief">{{ lv.brief }}</p>
        <div class="best-meta" v-if="starOf(lv.id) > 0">
          最佳 {{ (scores[lv.id].fuel_used * 1731.5).toFixed(1) }} km/s · {{ Math.round(scores[lv.id].elapsed_days) }} 天
          <span v-if="scores[lv.id].record_id" class="replayable-tag">可回放</span>
        </div>
        <div class="level-foot">
          <span class="stars">
            <template v-for="i in 3" :key="i">
              <span :class="i <= starOf(lv.id) ? 'lit' : 'dim'">★</span>
            </template>
          </span>
          <span v-if="!unlocked(lv.id)" class="lock-tag">🔒 先通关上一关</span>
          <span v-else class="go-btn">开始规划 →</span>
        </div>
      </div>
    </div>
  </div>`,
};
