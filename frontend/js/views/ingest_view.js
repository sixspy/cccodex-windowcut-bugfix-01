export const ingestViewTemplate = `
<div class="card" v-show="tab===1">
  <h2>写入事件</h2>
  <p class="muted">
    关窗禁写入册：已关闭窗后入账一律进迟到桶（乙路径），不进窗内合计。
    当日口径 {{policy.policy||'—'}} · 生效日 {{policy.effective_date||'—'}}
  </p>
  <label>窗口名</label><input v-model="form.window_name"/>
  <label>业务键</label><input v-model="form.biz_key"/>
  <label>事件时间</label><input v-model="form.event_ts"/>
  <label>金额</label><input type="number" step="0.01" v-model.number="form.amount"/>
  <button class="primary" @click="ingest">入账</button>
  <div class="err">{{actionErr}}</div>
</div>
<div class="card" v-show="tab===1">
  <h2>最近事件</h2>
  <table>
    <thead><tr><th>键</th><th>时间</th><th>金额</th><th>处置</th></tr></thead>
    <tbody>
      <tr v-for="e in (events||[]).slice(0,12)" :key="e.id">
        <td>{{e.biz_key}}</td>
        <td>{{e.event_ts}}</td>
        <td>{{e.amount}}</td>
        <td :class="e.disposition==='迟到桶'?'late':(e.disposition==='窗内'?'ok':'')">{{e.disposition}}</td>
      </tr>
    </tbody>
  </table>
</div>
`;
