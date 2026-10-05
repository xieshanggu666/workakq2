const { createApp } = Vue;

const app = createApp({
  data() {
    return {
      view: "levels",        // levels | challenges | game
      levelId: null,
      system: null,
      scores: {},
      challenges: [],
      challenge: null,       // 当前游玩的挑战详情（含当前版本定义）
    };
  },
  components: { LevelSelect, GameView, ChallengeSelect },
  async created() {
    try {
      this.system = await API.system();
      this.scores = this.system.scores || {};
    } catch (e) {
      this.system = null;
    }
  },
  computed: {
    level() {
      if (!this.system) return null;
      return this.system.levels.find(l => l.id === this.levelId) || null;
    },
    // 把挑战当前版本映射成 GameView 需要的关卡形态
    challengeLevel() {
      if (!this.challenge) return null;
      const v = this.challenge.current;
      return {
        id: this.challenge.id,
        name: this.challenge.title,
        brief: v.brief,
        milestones: v.milestones,
        budget_dv: v.budget_dv,
        t_max: v.t_max,
        hint: v.hint,
      };
    },
  },
  methods: {
    enter(id) {
      if (id > 5) return;
      this.challenge = null;
      this.levelId = id;
      this.view = "game";
    },
    back() { this.view = "levels"; },
    async openChallenges() {
      await this.refreshChallenges();
      this.view = "challenges";
    },
    async refreshChallenges() {
      try {
        this.challenges = (await API.challenges()).challenges || [];
      } catch (e) {
        this.challenges = [];
      }
    },
    async enterChallenge(c) {
      try {
        this.challenge = await API.challenge(c.id);
        this.view = "game";
      } catch (e) {
        alert("加载挑战失败：" + e.message);
      }
    },
    onGameBack() {
      // 挑战模式返回挑战列表（刷新解锁/待审状态），内置关卡返回选关
      if (this.challenge) {
        this.challenge = null;
        this.openChallenges();
      } else {
        this.back();
      }
    },
    onScore(s) {
      // 合并权威最佳成绩（含可回放记录 id），联动星级与关卡解锁
      const cur = this.scores[s.level_id] || {};
      this.scores = { ...this.scores, [s.level_id]: { ...cur, ...s } };
    },
    onChallengeChange() {
      // 挑战成绩提交后刷新列表（待审数等），审核联动在挑战列表页完成
      this.refreshChallenges();
    },
  },
  template: `
    <LevelSelect v-if="view === 'levels'" :system="system" :scores="scores"
                 @enter="enter" @challenges="openChallenges" />
    <ChallengeSelect v-else-if="view === 'challenges'"
                     :challenges="challenges" :bodies="system ? system.bodies : []"
                     @back="back" @enter="enterChallenge" @refresh="refreshChallenges" />
    <GameView v-else-if="view === 'game' && (challenge ? challengeLevel : level)"
              :key="challenge ? 'c' + challenge.id : levelId"
              :level="challenge ? challengeLevel : level"
              :bodies="system.bodies" :scores="scores" :challenge="challenge"
              @back="onGameBack" @enter="enter" @score="onScore"
              @challenge-change="onChallengeChange" />
  `,
});

app.mount("#app");
