import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};

export const reconciliations={
 list:()=>api('/reconciliations'),
 get:id=>api('/reconciliations/'+id),
 create:data=>api('/reconciliations',{method:'POST',body:JSON.stringify(data)}),
 ingest:(id,data)=>api('/reconciliations/'+id+'/sources',{method:'POST',body:JSON.stringify(data)}),
 sources:id=>api('/reconciliations/'+id+'/sources'),
 adjudicate:(id,data)=>api('/reconciliations/'+id+'/adjudications',{method:'POST',body:JSON.stringify(data)}),
 adjudications:id=>api('/reconciliations/'+id+'/adjudications'),
 publish:(id,data)=>api('/reconciliations/'+id+'/publish',{method:'POST',body:JSON.stringify(data)}),
 publications:id=>api('/reconciliations/'+id+'/publications'),
 trace:(id,batch)=>api('/reconciliations/'+id+'/trace?batch='+encodeURIComponent(batch))};
