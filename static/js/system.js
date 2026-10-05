/* 太阳系画布渲染：轨道/行星/轨迹/事件/探针动画 */
const System = {
  TWO_PI: Math.PI * 2,

  planetPos(b, t) {
    const ang = b.theta0 + this.TWO_PI * t / b.period;
    return { x: b.orbit * Math.cos(ang), y: b.orbit * Math.sin(ang) };
  },

  lerpTraj(pts, t) {
    if (!pts || pts.length === 0) return null;
    if (t <= pts[0][2]) return { x: pts[0][0], y: pts[0][1] };
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0, t0] = pts[i - 1], [x1, y1, t1] = pts[i];
      if (t <= t1) {
        const f = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
        return { x: x0 + (x1 - x0) * f, y: y0 + (y1 - y0) * f };
      }
    }
    const last = pts[pts.length - 1];
    return { x: last[0], y: last[1] };
  },

  draw(ctx, w, h, opts) {
    const { bodies, view, time, traj, events, level, probeGlow } = opts;
    ctx.clearRect(0, 0, w, h);
    this._stars(ctx, w, h);

    ctx.save();
    ctx.translate(view.cx, view.cy);
    const S = view.scale;

    // 太阳
    const sunR = Math.max(6, 0.00465 * S);
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, sunR * 3.2);
    g.addColorStop(0, "rgba(255,230,150,0.95)");
    g.addColorStop(0.4, "rgba(255,190,90,0.35)");
    g.addColorStop(1, "rgba(255,160,60,0)");
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(0, 0, sunR * 3.2, 0, this.TWO_PI); ctx.fill();
    ctx.fillStyle = "#ffd166";
    ctx.beginPath(); ctx.arc(0, 0, sunR, 0, this.TWO_PI); ctx.fill();

    // 轨道环 + 行星
    for (const b of bodies) {
      if (!b.orbit) continue;
      ctx.strokeStyle = "rgba(255,255,255,0.14)";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(0, 0, b.orbit * S, 0, this.TWO_PI); ctx.stroke();
      const p = this.planetPos(b, time);
      const r = Math.max(3, b.radius * S);
      ctx.fillStyle = b.color;
      ctx.beginPath(); ctx.arc(p.x * S, p.y * S, r, 0, this.TWO_PI); ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.75)";
      ctx.font = "12px 'Segoe UI', sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(b.name, p.x * S, p.y * S - r - 6);
    }

    // 弹弓影响球提示（当前选中行星时）
    if (opts.soiPlanet) {
      const b = opts.soiPlanet;
      const p = this.planetPos(b, time);
      ctx.strokeStyle = "rgba(120,220,255,0.25)";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.arc(p.x * S, p.y * S, b.soi * S, 0, this.TWO_PI); ctx.stroke();
      ctx.setLineDash([]);
    }

    // 里程碑目标区（内圈参考线）
    if (level && level.milestones.length) {
      const m = level.milestones[0];
      if (m.kind === "radius") {
        ctx.strokeStyle = "rgba(255,209,102,0.35)";
        ctx.setLineDash([6, 6]);
        ctx.beginPath(); ctx.arc(0, 0, m.r * S, 0, this.TWO_PI); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "rgba(255,209,102,0.85)";
        ctx.fillText(`目标半径 ${m.r} AU`, m.r * S - 40, -10);
      } else if (m.kind === "escape") {
        ctx.strokeStyle = "rgba(255,120,140,0.4)";
        ctx.setLineDash([6, 6]);
        ctx.beginPath(); ctx.arc(0, 0, m.r * S, 0, this.TWO_PI); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "rgba(255,120,140,0.9)";
        ctx.fillText(`逃逸边界 ${m.r} AU`, m.r * S - 50, -10);
      }
    }

    // 轨迹
    if (traj && traj.length > 1) {
      ctx.strokeStyle = "rgba(90,220,255,0.85)";
      ctx.lineWidth = 1.6;
      ctx.shadowColor = "rgba(90,220,255,0.8)";
      ctx.shadowBlur = 6;
      ctx.beginPath();
      for (let i = 0; i < traj.length; i++) {
        const [x, y] = traj[i];
        if (i === 0) ctx.moveTo(x * S, y * S);
        else ctx.lineTo(x * S, y * S);
      }
      ctx.stroke();
      ctx.shadowBlur = 0;

      // 事件标记
      if (events) {
        for (const e of events) {
          const p = this.lerpTraj(traj, e.t);
          if (!p) continue;
          if (e.type === "burn") {
            ctx.fillStyle = "#ffd166";
            ctx.beginPath();
            ctx.arc(p.x * S, p.y * S, 4.5, 0, this.TWO_PI);
            ctx.fill();
          } else if (e.type === "slingshot") {
            ctx.strokeStyle = "#7ce8b8";
            ctx.lineWidth = 1.6;
            ctx.beginPath();
            for (let k = 0; k < 5; k++) {
              const a = -Math.PI / 2 + k * (Math.PI * 4 / 5);
              const rr = 8;
              const sx = p.x * S + rr * Math.cos(a);
              const sy = p.y * S + rr * Math.sin(a);
              if (k === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
            }
            ctx.closePath(); ctx.stroke();
          }
        }
      }

      // 探针当前位置
      const p = this.lerpTraj(traj, time);
      if (p) {
        const pr = probeGlow || 5;
        ctx.fillStyle = "rgba(120,240,255,0.25)";
        ctx.beginPath(); ctx.arc(p.x * S, p.y * S, pr * 2.6, 0, this.TWO_PI); ctx.fill();
        ctx.fillStyle = "#eafcff";
        ctx.beginPath(); ctx.arc(p.x * S, p.y * S, pr, 0, this.TWO_PI); ctx.fill();
      }
    }
    ctx.restore();
  },

  _stars(ctx, w, h) {
    if (!this._starPts) {
      this._starPts = [];
      for (let i = 0; i < 90; i++) {
        this._starPts.push({
          x: Math.random() * w, y: Math.random() * h,
          r: Math.random() * 1.2 + 0.3,
          a: Math.random() * 0.5 + 0.25,
        });
      }
    }
    for (const s of this._starPts) {
      ctx.fillStyle = `rgba(255,255,255,${s.a})`;
      ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, this.TWO_PI); ctx.fill();
    }
  },
};
