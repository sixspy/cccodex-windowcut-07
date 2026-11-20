export const auditViewTemplate = `
<div class="card" v-show="tab===5">
  <h2>审计流水</h2>
  <table>
    <thead><tr><th>时间</th><th>人</th><th>动作</th><th>目标</th></tr></thead>
    <tbody>
      <tr v-for="a in audits.slice(0,20)" :key="a.id">
        <td>{{a.created_at}}</td>
        <td>{{a.actor}}</td>
        <td>{{a.action}}</td>
        <td>{{a.target}}</td>
      </tr>
    </tbody>
  </table>
</div>
<div class="card" v-show="tab===5">
  <h2>争用 / 锁</h2>
  <p class="muted">争用 {{contention.length}} · 持锁 {{locks.length}}</p>
  <table>
    <thead><tr><th>锁键</th><th>角色</th><th>动作</th><th>时间</th></tr></thead>
    <tbody>
      <tr v-for="c in contention.slice(0,15)" :key="'c'+c.id">
        <td>{{c.lock_key}}</td>
        <td>{{c.actor}}</td>
        <td>{{c.action}}</td>
        <td>{{c.created_at}}</td>
      </tr>
    </tbody>
  </table>
  <h2 style="margin-top:16px">旁路暂存</h2>
  <label>窗口</label><input v-model="holdForm.window_name"/>
  <label>业务键</label><input v-model="holdForm.biz_key"/>
  <label>时间</label><input v-model="holdForm.event_ts"/>
  <label>金额</label><input type="number" v-model.number="holdForm.amount"/>
  <button class="primary" @click="createHold">写入暂存</button>
  <p class="muted">
    写路径口径（当天禁改口）：
    <b v-for="r in promotionRegistry" :key="r.id">
      {{r.path}}路径 · {{r.register_day}} · {{r.actor}}
    </b>
    <span v-if="!promotionRegistry.length">尚未择定，首次晋升入册</span>
  </p>
  <p class="muted">
    五处同态巡检：
    <b :class="crosscheck.consistent===false?'late':'ok'">
      {{crosscheck.consistent===false ? '撕裂 ' + crosscheck.violations.length + ' 笔' : '同态一致'}}
    </b>
    <span v-for="v in (crosscheck.violations||[]).slice(0,5)" :key="v.hold_id"
          style="margin-left:8px;color:#b04a4a">
      #{{v.hold_id}} {{v.kind}}（{{v.biz_key}}）
    </span>
  </p>
  <table>
    <thead><tr><th>ID</th><th>键</th><th>金额</th><th>状态</th><th>操作</th></tr></thead>
    <tbody>
      <tr v-for="h in holds" :key="'h'+h.id">
        <td>{{h.id}}</td><td>{{h.biz_key}}</td><td>{{h.amount}}</td><td>{{h.status}}</td>
        <td>
          <button class="primary" @click="promoteHold(h.id)">晋升（甲·同事务）</button>
          <button @click="promoteDraft(h.id,h)" title="乙路径影子单，册子择甲后应被拒">草稿（乙·验偏册）</button>
        </td>
      </tr>
      <tr v-if="!holds.length"><td colspan="5" class="muted">活跃暂存为空（已晋升笔不在此口径，仅留审计痕迹）</td></tr>
    </tbody>
  </table>
</div>
`;
