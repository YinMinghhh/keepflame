"""Synthetic reconstruction of the cached official credits component contract.

Source: chunk-824f8cab.js?fe-v=1789963951000, SHA256
de672a04511bcf4c29714dc88074b0e45352cf78401fa1434d869f7ecc44e08a.
Modules 438/665 each fetch task info in created(); module 531 selects slots
for undefined/0/1; module 667 supplies the reading title, labels and callback.
This is not captured account HTML and does not execute the vendor bundle.
"""


def credits_document(piece, case):
    return r'''<!doctype html><meta charset="utf-8"><title>Synthetic credits contract</title>
      <div class="floor-credit-card"><div class="el-card__header"><span class="header-title">Other task</span></div><button>立即领取</button></div>
      <div class="floor-credit-center">
        <div class="floor-credit-card" id="reading-card">
          <div class="el-card__header"><span class="header-title">浏览社区文章</span><span class="header-credit">+1</span></div>
          <div class="el-card__body"><div class="card-body">浏览1篇文章可以获得1积分，每月最多可得30分</div>
            <div class="card-footer"><span class="will-expire"></span><div class="card-next" id="actions"></div></div>
          </div>
        </div>
      </div>
      <div id="yth_captchar" style="display:none"><div id="xy_img" style="width:56px;height:56px"></div></div>
      <script>
      const scenario = 'CASE';
      const creditCenter = {
        taskList: {post_view:{}}, curRuleKey:'', tokenType:'',
        handleReceiveCredit: async function(rule, token) {
          if (!token) {
            this.curRuleKey = scenario === 'wrong_callback' ? 'login' : rule;
            this.tokenType = 'receiveCredits';
            await fetch('/common/verifyCode/captchar', {method:'POST'});
            document.querySelector('#xy_img').style.backgroundImage = 'url("PIECE")';
            document.querySelector('#yth_captchar').style.display = 'block';
            return;
          }
          await fetch('/user/account/getToken');
          await fetch('/credits/task/receiveCredits', {method:'POST',
            headers:{'Content-Type':'application/x-www-form-urlencoded'},
            body:new URLSearchParams({ruleKey:scenario === 'wrong_rule' ? 'login' : rule,
              valideCode:token,appKey:'synthetic',appSecret:'synthetic'})});
          // Official success callbacks refresh child and parent. These page reads
          // must remain blocked during confirmation; the Keeper uses its own GET.
          this.fetchTaskInfo(); floor.fetchTaskInfo();
        },
        getToken: function(value) { this.handleReceiveCredit(this.curRuleKey, value).catch(()=>{}); },
        fetchTaskInfo: function() {
          fetch('/credits/task/getTaskInfo').then(r=>r.json()).then(body=>{
            this.taskList = Object.assign({post_view:{}}, body.data.taskList);
            render(this.taskList.post_view.status);
          }).catch(()=>{});
        }
      };
      const floor = {fetchTaskInfo: function() {
        fetch('/credits/task/getTaskInfo').then(r=>r.json()).then(body=>{
          this.isSignin = body.data.taskList.login?.status == 1;
        }).catch(()=>{});
      }};
      function render(status) {
        const actions = document.querySelector('#actions'); actions.replaceChildren();
        const button = document.createElement('button');
        const span = document.createElement('span'); button.appendChild(span);
        button.className = 'el-button el-button--mini';
        if (status == undefined) {
          span.textContent = '去看看'; const link = document.createElement('a');
          link.href = '/view/community/list?listType=1'; link.target = '_blank';
          actions.appendChild(link); link.appendChild(button);
        } else if (status == 0) {
          // Element UI puts the slot text (including source newlines) in a span.
          span.textContent = ['changed_text', 'cdp_unavailable'].includes(scenario) ? 'SYNTHETIC_PRIVATE' : '立即领取\r\n                ';
          actions.appendChild(button);
          button.__vue__ = {$parent: {$parent: creditCenter}};
          button.onclick = () => creditCenter.handleReceiveCredit('post_view').catch(()=>{});
          if (scenario === 'disabled_button') button.disabled = true;
          if (scenario === 'hidden_button') button.style.display = 'none';
          if (scenario === 'duplicate_button') actions.appendChild(button.cloneNode(true));
        } else if (status == 1) {
          span.textContent = '已完成'; button.disabled = true; actions.appendChild(button);
        }
        if (scenario === 'duplicate_card' && !document.querySelector('#duplicate')) {
          const clone = document.querySelector('#reading-card').cloneNode(true);
          clone.id = 'duplicate'; document.body.appendChild(clone);
        }
      }
      render(undefined);
      // Parent created() precedes the child's created(), and both consume native
      // status responses. A one-read gate starves the child's default slot.
      if (scenario === 'child_first') { creditCenter.fetchTaskInfo(); floor.fetchTaskInfo(); }
      else { floor.fetchTaskInfo(); creditCenter.fetchTaskInfo(); }
      if (scenario === 'third_status') fetch('/credits/task/getTaskInfo').catch(()=>{});
      if (scenario === 'credits_unknown_write') fetch('/synthetic/unexpected-write', {method:'POST'}).catch(()=>{});
      </script>'''.replace('CASE', case).replace('PIECE', piece)
