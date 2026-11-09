'use strict';
const crypto=require('node:crypto');
const {HubError}=require('./remote.cjs');
class AgentRuntime{
  constructor(controller){this.c=controller;}
  start(){if(!this.timer){this.timer=setInterval(()=>this.tick().catch(()=>{}),30000);this.timer.unref?.();}}
  async drain(){clearInterval(this.timer);this.timer=null;await this.active?.catch(()=>{});}
  async invoke(binding,action,args){
    require('./agent-tools.cjs').validate(action,args);
    const c=this.c,p=c.profile,remote=c.remote;
    if(!p || c.connection!=='active' || binding.status!=='active')throw new HubError('LOGIN_REQUIRED');
    if(action==='hub_register_agent'){
      const result=await c.invokeProjectRemote(binding,action,args);c.ensureCurrent(p,remote);
      p.agentEndpoints??={};p.agentEndpoints[result.id]={binding_id:binding.id,endpoint:result};
      await c.persist();c.emitState();this.start();return result;
    }
    const saved=p.agentEndpoints?.[args.endpoint_id];
    if(!saved || saved.binding_id!==binding.id)throw new HubError('AGENT_NOT_ACCESSIBLE');
    const result=await c.invokeProjectRemote(binding,'hub_list_agent_dispatches',args);c.ensureCurrent(p,remote);
    const slot=await c.projectSlot(binding.projectId);
    await remote.request('POST',`/api/projects/${binding.projectId}/agents/${args.endpoint_id}/heartbeat`,
      {expected_version:saved.endpoint.version},{slot,idempotency:crypto.randomUUID()});c.ensureCurrent(p,remote);
    saved.endpoint.last_seen_at=new Date().toISOString();await c.persist();
    return result;
  }
  tick(){
    if(this.active)return this.active;
    const job=this.cycle();this.active=job;
    return job.finally(()=>{if(this.active===job)this.active=null;});
  }
  async cycle(){
    const c=this.c,p=c.profile,remote=c.remote;
    if(!p || c.closed || c.draining || c.connection!=='active')return;
    for(const saved of Object.values(p.agentEndpoints || {})){
      const binding=p.bindings.find(b=>b.id===saved.binding_id && b.status==='active');
      if(!binding || !saved.endpoint.enabled || saved.stopped)continue;
      try{
        // Passive desktop polling must never send heartbeat/receive/accept.
        const result=await c.invokeProjectRemote(binding,'hub_list_agent_dispatches',{endpoint_id:saved.endpoint.id,limit:100});
        c.ensureCurrent(p,remote);
        const pending=result.items.filter(x=>['queued','received'].includes(x.state) && !x.expired);
        const unseen=pending.filter(x=>!(saved.notified || []).includes(x.id));
        saved.pending_count=pending.length;saved.inbox_checked_at=new Date().toISOString();saved.error=null;
        if(unseen.length)c.emit('notification',{title:'收到新的协作待办',body:`${saved.endpoint.name} 有 ${unseen.length} 项待办，请在开发工具中核对并接受。`});
        saved.notified=[...new Set([...(saved.notified || []),...pending.map(x=>x.id)])].slice(-200);
        // A guide has its own lease after development ends; desktop polling never claims it.
        const guides=[];
        for(const state of ['queued','running']){
          const page=await c.invokeProjectRemote(binding,'hub_list_guides',{instance_id:saved.endpoint.instance_id,state,limit:100});
          c.ensureCurrent(p,remote);
          guides.push(...page.items.filter(x=>x.state==='queued' || Date.parse(x.lease_until)<=Date.now()));
        }
        const newGuides=guides.filter(x=>!(saved.guide_notified || []).includes(x.id+':'+x.version));
        saved.pending_guides=guides.length;
        if(newGuides.length)c.emit('notification',{title:'功能说明需要补齐',body:`${saved.endpoint.name} 有 ${newGuides.length} 份操作说明待办，请在功能操作说明页面核对。`});
        saved.guide_notified=[...new Set([...(saved.guide_notified || []),...guides.map(x=>x.id+':'+x.version)])].slice(-200);
      }catch(error){
        if(c.profile!==p || c.remote!==remote)return;
        saved.error=error.code || 'AGENT_INBOX_UNAVAILABLE';
      }
    }
    if(c.profile===p){await c.persist();c.emitState();}
  }
}
module.exports={AgentRuntime};
