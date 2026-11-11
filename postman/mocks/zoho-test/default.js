// Captured test-report shape, with Added_Time adapted to the user-confirmed IST contract.
// Both time fields use IST; all phone values are fictional.
// Only OAuth refresh and the two read-only report routes are implemented.
const http = require("http");
const records = {
  "Plan_Report": [
    {
      "Modified_Time": "21-Oct-2026 13:13:47",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-H",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P",
      "ID": "504291000000016018",
      "PatientName": "Fictional Child H",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:13:47"
    },
    {
      "Modified_Time": "21-Oct-2026 13:13:09",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-G",
      "Schedule": "IEP Session, 2, 3, TEST CONSULTANT I",
      "ID": "504291000000016016",
      "PatientName": "Fictional Child G",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:13:09"
    },
    {
      "Modified_Time": "21-Oct-2026 13:12:41",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-F",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P",
      "ID": "504291000000016014",
      "PatientName": "Fictional Child F",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:12:41"
    },
    {
      "Modified_Time": "21-Oct-2026 19:26:09",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-E",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P; IEP Session, 2, 3, TEST CONSULTANT I",
      "ID": "504291000000016012",
      "PatientName": "Fictional Child E",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:12:09"
    },
    {
      "Modified_Time": "21-Oct-2026 13:11:19",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-D",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P",
      "ID": "504291000000016010",
      "PatientName": "Fictional Child D",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:11:19"
    },
    {
      "Modified_Time": "21-Oct-2026 13:10:50",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-C",
      "Schedule": "IEP Session, 2, 3, TEST CONSULTANT I",
      "ID": "504291000000016008",
      "PatientName": "Fictional Child C",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:10:50"
    },
    {
      "Modified_Time": "21-Oct-2026 13:10:25",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-B",
      "Schedule": "Psychology Therapy Session, 2, 3, TEST CONSULTANT P",
      "ID": "504291000000016006",
      "PatientName": "Fictional Child B",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:10:25"
    },
    {
      "Modified_Time": "21-Oct-2026 19:25:55",
      "Branch": "Malaparamba EIC",
      "RegistrationID": "TEST-A",
      "Schedule": "Psychology Therapy Session, 3, 3, TEST CONSULTANT P; IEP Session, 2, 3, TEST CONSULTANT I",
      "ID": "504291000000016002",
      "PatientName": "Fictional Child A",
      "Parent_No": "919999999999",
      "Added_Time": "21-Oct-2026 13:08:51"
    }
  ],
  "All_Consultants": [
    {
      "Status": "Active",
      "Department": "IEP",
      "Modified_Time": "21-Oct-2026 13:16:34",
      "ConsultationType": "IEP Session",
      "Branch": "Malaparamba EIC",
      "Phone_Number": "919999999998",
      "ID": "504291000000016022",
      "Name": "TEST CONSULTANT I",
      "Added_Time": "21-Oct-2026 13:16:34"
    },
    {
      "Status": "Active",
      "Department": "Psychology",
      "Modified_Time": "21-Oct-2026 13:15:59",
      "ConsultationType": "Psychology Therapy Session",
      "Branch": "Malaparamba EIC",
      "Phone_Number": "919999999998",
      "ID": "504291000000016020",
      "Name": "TEST CONSULTANT P",
      "Added_Time": "21-Oct-2026 13:14:34"
    }
  ]
}
;
const wallTime = value => {
  const [day, month, year, hour, minute, second] = value.split(/[- :]/);
  return Date.UTC(Number(year), ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"].indexOf(month), Number(day), Number(hour), Number(minute), Number(second));
};
const indiaWallTime = utcValue => {
  const date = new Date(wallTime(utcValue) + 330 * 60000);
  const pad = n => String(n).padStart(2, "0");
  return `${pad(date.getUTCDate())}-${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][date.getUTCMonth()]}-${date.getUTCFullYear()} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
};
for (const rows of Object.values(records)) {
  for (const row of rows) {
    row.Added_Time = indiaWallTime(row.Added_Time);
    row.Modified_Time = indiaWallTime(row.Modified_Time);
  }
}
// Expanded source contract: stable fields, extra columns and phones stored only
// on registrations. No Modified_Time is exposed by these fictional reports.
records.Mapped_Plans = records.Plan_Report.map(({Parent_No, Modified_Time, PatientName, RegistrationID, ...row}) =>
  ({...row, Name:PatientName, Registration_Id:RegistrationID, Extra_Plan_Column:{ignored:true}}));
records.Mapped_Consultants = records.All_Consultants.map(({Modified_Time, ...row}) =>
  ({...row, Extra_Consultant_Column:"ignored"}));
records.All_Registrations = records.Plan_Report.map((row, index) => ({
  ID:String(980000 + index), Registration_Id:row.RegistrationID, Phone:row.Parent_No,
  Added_Time:row.Added_Time, Extra_Registration_Column:["ignored"]
}));
records.Scheduler_Report = [{ID:"950000",Reg_ID:"DEMO",PatientName:"Fictional Existing Row",Doctor:"Demo Doctor",
  Therapy_Date:"24-10-2026",TherapyTime:"09:00 AM",Scheduler_Key:"",Status:"",Added_Time:"23-Oct-2026 11:05:33"}];
let schedulerSequence = 950000, failAfterCreate = false, schedulerKeyUnique = true;
let fault = null;
let rejectSchedulerIndex = null;
// Fictional structured Thanal contract. Never contains captured patient data.
const calicut = {ID:"10",Branch_Name:"Iqraa Thanal Super Speciality EIC Centre Calicut"};
const otherBranch = {ID:"11",Branch_Name:"Other fictional centre"};
const thanalReports = {
  All_Consultants: [
    {ID:"200",Name:"Fictional clinician",Status:"Active",Branch:calicut,Consultation_Type:"Full-Time",
     Consultations:[{ID:"20",Value:"Speech"},{ID:"21",Value:"Occupational"}]},
    {ID:"201",Name:"Other fictional clinician",Status:"Active",Branch:otherBranch,
     Consultations:[{ID:"20",Value:"Speech"}]}
  ],
  All_Registrations: [
    {ID:"100",Registration_Id:"FAKE-1",Name2:"Fictional child",Phone_Number:"919999999999",
     Branch:calicut,Added_Time:"01/11/2026 08:00:00"},
    {ID:"101",Registration_Id:"TEST-UNUSED",Name2:"Unused fictional child",Phone_Number:"123",
     Added_Time:"01/11/2026 08:00:00"}
  ],
  All_Therapy_Schedule_Plans: [
    {ID:"300",Branch:calicut,Name:{ID:"100",Name2:"Fictional child"},Status:"Draft",
     Added_Time:"01/11/2026 09:00:00",Schedule:[
       {ID:"400",Therapy:{ID:"20",Value:"Speech"},Consultant:{ID:"200"},Per_Week:"2",No_Of_Week:"3"},
       {ID:"401",Therapy:{ID:"21",Value:"Occupational"},Consultant:{ID:"200"},Per_Week:"2",No_Of_Week:"3"}]},
    {ID:"301",Branch:otherBranch,Name:{ID:"101"},Status:"Draft",Added_Time:"01/11/2026 09:00:00",Schedule:[]}
  ]
};
const thanalAppointments = [];
const thanalFields = ['Branch','Registration','For_Parent','Date_field','Department','Consultation_Type',
  'Consultant','Appointment_Time_Slot','Slot_Number','Status','Scheduler_Key','Date_Time','End_Time','Duration'];
const thanalLookups = ['Branch','Registration','Department','Consultation_Type','Consultant','Appointment_Time_Slot'];
http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  // A separate fictional main-workflow namespace exercises target configuration.
  // This alias exists only in the local mock, never in application code.
  url.pathname = url.pathname.replace('/fictional-clinic/eic-pilot/', '/nidhin.kumar_turnb/test-gokul/');
  res.setHeader("Content-Type", "application/json");
  if (req.method === "POST" && url.pathname === "/__test__/fault") {
    fault = req.body?.count ? {...req.body} : null;
    res.end(JSON.stringify({armed:Boolean(fault)})); return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/reject-scheduler-row") {
    rejectSchedulerIndex = Number.isInteger(req.body?.index) ? req.body.index : null;
    res.end(JSON.stringify({armed:rejectSchedulerIndex !== null}));return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/registration-phone") {
    const row = records.All_Registrations.find(item => item.Registration_Id === req.body?.registration);
    if (!row || !/^[1-9]\d{7,14}$/.test(req.body?.phone || "")) {
      res.writeHead(400);res.end(JSON.stringify({message:"Invalid fictional registration contact"}));return;
    }
    row.Phone = req.body.phone;
    res.end(JSON.stringify({updated:true}));return;
  }
  const activeFault = fault && url.pathname.endsWith(fault.path) &&
    (!fault.cursor || req.headers.record_cursor === fault.cursor) && fault.count > 0 ? fault : null;
  if (activeFault && !activeFault.afterWrite) {
    activeFault.count--;
    res.writeHead(activeFault.status || 503, {"Retry-After":String(activeFault.retryAfter || 0)});
    res.end(JSON.stringify(Object.hasOwn(activeFault, "payload") ? activeFault.payload :
      {code:activeFault.status === 429 ? 2955 : 500})); return;
  }
  const thanalReport = url.pathname.match(/^\/creator\/v2\.1\/data\/thanal\/eic\/report\/(\w+)$/);
  const thanalBase = '/creator/v2.1/data/thanal/eic/';
  if (req.method === 'GET' && url.pathname === '/creator/v2.1/meta/thanal/eic/form/Schedule_Adhoc_Trial/fields') {
    res.end(JSON.stringify({code:3000,fields:thanalFields.map(link_name => ({link_name,
      type:thanalLookups.includes(link_name) || link_name==='Status' ? 12 :
        ({For_Parent:16,Date_field:10,Slot_Number:5,Date_Time:11,End_Time:11,Duration:5}[link_name] || 1),
      is_lookup_field:thanalLookups.includes(link_name),unique:link_name==='Scheduler_Key',max_char:255,
      choices:link_name==='Status' ? ['Scheduled','Completed','Cancelled','Recreate'].map(value=>({value,key:value})) : undefined}))}));return;
  }
  const thanalSingle = url.pathname.match(/^\/creator\/v2\.1\/data\/thanal\/eic\/report\/Schedule_Adhoc_Trial_Report\/(\d+)$/);
  const thanalCreate = req.method==='POST' && url.pathname===thanalBase+'form/Schedule_Adhoc_Trial';
  const thanalUpdate = req.method==='PATCH' && (thanalSingle || url.pathname===thanalBase+'report/Schedule_Adhoc_Trial_Report');
  if (req.method==='GET' && url.pathname===thanalBase+'report/Schedule_Adhoc_Trial_Report') {
    const keys=[...(url.searchParams.get('criteria')||'').matchAll(/Scheduler_Key == "([^"]+)"/g)].map(m=>m[1]);
    if (!keys.length) {res.writeHead(400);res.end(JSON.stringify({code:3330}));return;}
    const data=thanalAppointments.filter(row=>keys.includes(row.Scheduler_Key)).map(row=>{
      const result={...row,For_Parent:String(row.For_Parent),Slot_Number:String(row.Slot_Number),
        Date_Time:row.Date_Time+':00',Duration:String(row.Duration)};
      for (const field of thanalLookups) result[field]={ID:row[field],zc_display_value:'Fictional display',
        ...(field==='Registration' ? {Name2:'Fictional child'} : {})};
      return result;
    });
    res.end(JSON.stringify({code:3000,data}));return;
  }
  if (thanalCreate || thanalUpdate) {
    const bulk=Array.isArray(req.body?.data);
    const ids=thanalSingle ? [thanalSingle[1]] : [...(req.body?.criteria||'').matchAll(/ID == (\d+)/g)].map(m=>m[1]);
    const selected=thanalUpdate ? thanalAppointments.filter(row=>ids.includes(row.ID)) :
      (bulk ? req.body.data : [req.body?.data]);
    const result=selected.map((old,index)=>{
      const data=thanalUpdate ? {...old,...req.body.data} : old;
      const actualKeys=Object.keys(data||{}).filter(k=>k!=='ID').sort().join();
      const start = data?.Appointment_Time_Slot==='700' ? '09:00' : '09:40';
      const end = data?.Appointment_Time_Slot==='700' ? '09:40:00' : '10:20:00';
      if (data?.Date_Time!==data?.Date_field+' '+start || data?.End_Time!==data?.Date_field+' '+end || data?.Duration!==40)
        return {code:3001,message:'Date_Time, End_Time and Duration are required by the session workflow'};
      if (actualKeys!==[...thanalFields].sort().join() || req.body.skip_workflow || data.For_Parent!==false ||
          !data.Scheduler_Key?.startsWith('main-') || data.Branch!=='10' || data.Registration!=='100' ||
          data.Consultant!=='200' || !['20','21'].includes(data.Consultation_Type) ||
          !['900','901'].includes(data.Department) || !['700','701'].includes(data.Appointment_Time_Slot) ||
          !['Scheduled','Completed','Cancelled'].includes(data.Status))
        return {code:3001,message:'Invalid fictional Thanal lookup payload'};
      if (thanalCreate && index===rejectSchedulerIndex) return {code:3001,message:'Fictional form validation rejection'};
      if (thanalCreate && thanalAppointments.some(row=>row.Scheduler_Key===data.Scheduler_Key))
        return {code:3001,message:'Duplicate Scheduler Key'};
      const row=thanalUpdate ? Object.assign(old,req.body.data) : {ID:String(++schedulerSequence),...data};
      if (thanalCreate) thanalAppointments.push(row);
      return {code:3000,data:{ID:row.ID,Scheduler_Key:row.Scheduler_Key}};
    });
    rejectSchedulerIndex=null;
    if (activeFault && activeFault.afterWrite) {
      activeFault.count--;
      if (activeFault.disconnect) {req.socket.destroy();return;}
      res.writeHead(503);res.end(JSON.stringify({code:500}));return;
    }
    res.end(JSON.stringify(bulk || !thanalSingle ? {code:3000,result:result.reverse()} : result[0]));return;
  }
  if (req.method === "GET" && thanalReport && thanalReports[thanalReport[1]]) {
    const report = thanalReport[1];
    if (url.searchParams.get("field_config") !== "all" ||
        (report === "All_Therapy_Schedule_Plans" && url.searchParams.get("criteria") !== "Branch == 10") ||
        (report !== "All_Therapy_Schedule_Plans" && url.searchParams.has("criteria"))) {
      res.writeHead(400); res.end(JSON.stringify({code:3330,message:"Unexpected structured snapshot query"}));return;
    }
    let data = thanalReports[report];
    if (report === "All_Therapy_Schedule_Plans") data = data.filter(r => r.Branch.ID === "10");
    if (report === "All_Registrations") {
      if (!req.headers.record_cursor) {res.setHeader("record_cursor","thanal-page-2");data=data.slice(0,1);}
      else if (req.headers.record_cursor === "thanal-page-2") data=data.slice(1);
      else {res.writeHead(400);res.end(JSON.stringify({code:3330}));return;}
    }
    res.end(JSON.stringify({code:3000,data}));return;
  }
  const schedulerBase = "/creator/v2.1/data/nidhin.kumar_turnb/test-gokul/";
  if (req.method === "GET" && url.pathname === "/creator/v2.1/meta/nidhin.kumar_turnb/test-gokul/form/Scheduler/fields") {
    res.end(JSON.stringify({code:3000,fields:["Reg_ID","PatientName","Doctor","Therapy_Date","TherapyTime","Scheduler_Key","Status"].map(link_name =>
      ({link_name,type:1,max_char:255,unique:link_name === "Scheduler_Key" && schedulerKeyUnique}))}));return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/scheduler-key-unique") {
    schedulerKeyUnique = req.body?.enabled === true;res.end(JSON.stringify({enabled:schedulerKeyUnique}));return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/fail-next-scheduler-create") {
    failAfterCreate = true; res.end(JSON.stringify({armed:true})); return;
  }
  if (req.method === "GET" && url.pathname === schedulerBase + "report/Scheduler_Report") {
    const criteria = url.searchParams.get("criteria") || "";
    const keys = [...criteria.matchAll(/Scheduler_Key == "([^"]+)"/g)].map(item => item[1]);
    if (url.searchParams.get("field_config") !== "all" || (criteria !== "ID != 0" && !keys.length)) {
      res.writeHead(400); res.end(JSON.stringify({code:3330})); return;
    }
    res.end(JSON.stringify({code:3000,data:keys.length ? records.Scheduler_Report.filter(r => keys.includes(r.Scheduler_Key)) : records.Scheduler_Report})); return;
  }
  if ((req.method === "POST" && url.pathname === schedulerBase + "form/Scheduler" && Array.isArray(req.body?.data)) ||
      (req.method === "PATCH" && url.pathname === schedulerBase + "report/Scheduler_Report")) {
    const updating = req.method === "PATCH";
    const ids = [...(req.body.criteria || "").matchAll(/ID == (\d+)/g)].map(item => item[1]);
    const input = updating ? records.Scheduler_Report.filter(row => ids.includes(row.ID)) : req.body.data;
    if (!input.length || input.length > 200 || req.body.skip_workflow) {
      res.writeHead(400); res.end(JSON.stringify({code:3001})); return;
    }
    const result = input.map((data, index) => {
      if (!updating && index === rejectSchedulerIndex) return {code:3001,message:"Fictional form validation rejection"};
      if (!updating && records.Scheduler_Report.some(r => r.Scheduler_Key === data.Scheduler_Key))
        return {code:3001,message:"Duplicate Scheduler Key"};
      const row = updating ? Object.assign(data, req.body.data) : {ID:String(++schedulerSequence),...data};
      if (!updating) records.Scheduler_Report.push(row);
      return {code:3000,data:{ID:row.ID,Scheduler_Key:row.Scheduler_Key}};
    });
    rejectSchedulerIndex = null;
    if (failAfterCreate && !updating) {failAfterCreate=false;res.writeHead(500);res.end(JSON.stringify({code:500}));return;}
    if (activeFault && activeFault.afterWrite) {
      activeFault.count--;
      if (activeFault.disconnect) {req.socket.destroy();return;}
      if (activeFault.delayMs) {setTimeout(() => res.end(JSON.stringify({code:3000,result})), activeFault.delayMs);return;}
      res.writeHead(503);res.end(JSON.stringify({code:500}));return;
    }
    res.end(JSON.stringify({code:3000,result:result.reverse()}));return;
  }
  const schedulerUpdate = url.pathname.match(/^\/creator\/v2\.1\/data\/nidhin\.kumar_turnb\/test-gokul\/report\/Scheduler_Report\/(\d+)$/);
  if ((req.method === "POST" && url.pathname === schedulerBase + "form/Scheduler") || (req.method === "PATCH" && schedulerUpdate)) {
    const data = req.body?.data;
    const fields = ["Reg_ID","PatientName","Doctor","Therapy_Date","TherapyTime","Scheduler_Key","Status"];
    if (!data || Object.keys(data).sort().join() !== fields.sort().join() || !/^(test|main)-/.test(data.Scheduler_Key) || req.body.skip_workflow) {
      res.writeHead(400); res.end(JSON.stringify({code:3001})); return;
    }
    let row;
    if (schedulerUpdate) {
      row = records.Scheduler_Report.find(r => r.ID === schedulerUpdate[1] && r.Scheduler_Key === data.Scheduler_Key);
      if (!row) {res.writeHead(400);res.end(JSON.stringify({code:3100}));return;}
      Object.assign(row, data);
    } else {
      if (records.Scheduler_Report.some(r => r.Scheduler_Key === data.Scheduler_Key)) {
        res.writeHead(400);res.end(JSON.stringify({code:3001,message:"Duplicate Scheduler Key"}));return;
      }
      row = {ID:String(++schedulerSequence),...data,Added_Time:"23-Oct-2026 12:00:00"};
      records.Scheduler_Report.push(row);
      if (failAfterCreate) {failAfterCreate=false;res.writeHead(500);res.end(JSON.stringify({code:500}));return;}
    }
    if (activeFault && activeFault.afterWrite) {
      activeFault.count--;
      // The provider committed but the client cannot see the response.
      if (activeFault.disconnect) {req.socket.destroy();return;}
      if (activeFault.delayMs) {
        setTimeout(() => res.end(JSON.stringify({code:3000,data:{ID:row.ID}})), activeFault.delayMs);
        return;
      }
      res.writeHead(503);res.end(JSON.stringify({code:500}));return;
    }
    res.end(JSON.stringify({code:3000,data:{ID:row.ID}}));return;
  }
  // Local fixture control, not a Zoho API route. Only dummy contact data is added.
  if (req.method === "POST" && url.pathname === "/__test__/add-patient") {
    const stamp = req.body?.at;
    const parent = req.body?.phone || "919999999999";
    if (typeof stamp !== "string" || !/^[1-9]\d{7,14}$/.test(parent)) {
      res.writeHead(400); res.end(JSON.stringify({message:"Supply at timestamp"})); return;
    }
    if (!records.Plan_Report.some(row => row.ID === "900001")) {
      records.Plan_Report.push({ID:"900001", RegistrationID:"NEW-009", PatientName:"New Fictional Child",
        Branch:"Malaparamba EIC", Parent_No:parent, Added_Time:indiaWallTime(stamp), Modified_Time:indiaWallTime(stamp),
        Schedule:"Psychology Therapy Session, 1, 3, New Clinician"});
      records.All_Consultants.push({ID:"900002", Name:"New Clinician", Branch:"Malaparamba EIC",
        Department:"Psychology", ConsultationType:"Psychology Therapy Session", Status:"Active",
        Phone_Number:"919999999998", Added_Time:indiaWallTime(stamp), Modified_Time:indiaWallTime(stamp)});
    }
    res.end(JSON.stringify({added:true})); return;
  }
  if (req.method === "POST" && url.pathname === "/oauth/v2/token") {
    const scope = "ZohoCreator.form.CREATE ZohoCreator.report.READ ZohoCreator.report.UPDATE ZohoCreator.meta.form.READ";
    const grant = req.body?.grant_type || new URLSearchParams(typeof req.body === "string" ? req.body : "").get("grant_type");
    res.end(JSON.stringify({access_token: "simulation-token", expires_in: 3600, scope,
      ...(grant === "authorization_code" ? {refresh_token:"simulation-refresh"} : {})})); return;
  }
  if (req.method === "GET" && url.pathname.startsWith("/creator/v2.1/data/nidhin.kumar_turnb/test-gokul/report/")) {
    const report = url.pathname.split("/").pop();
    if (!records[report]) { res.writeHead(501); res.end(JSON.stringify({message: "Endpoint not modeled"})); return; }
    if (url.searchParams.get("field_config") !== "all") {
      res.writeHead(400); res.end(JSON.stringify({message: "Expected field_config=all"})); return;
    }
    let data = records[report];
    const criteria = url.searchParams.get("criteria");
    if (criteria) {
      const addedEnd = criteria.match(/Added_Time < '([^']+)'/);
      const modifiedEnd = criteria.match(/Modified_Time < '([^']+)'/);
      const addedStart = criteria.match(/Added_Time >= '([^']+)'/);
      const modifiedStart = criteria.match(/Modified_Time >= '([^']+)'/);
      if (!addedEnd || Boolean(addedStart) !== Boolean(modifiedStart) || (addedStart && !modifiedEnd)) {
        res.writeHead(400); res.end(JSON.stringify({code:3330,message:"Unsupported criteria"})); return;
      }
      data = data.filter(row => wallTime(row.Added_Time) < wallTime(addedEnd[1]) &&
        (!modifiedEnd || wallTime(row.Modified_Time) < wallTime(modifiedEnd[1])) &&
        (!addedStart || wallTime(row.Added_Time) >= wallTime(addedStart[1]) ||
          wallTime(row.Modified_Time) >= wallTime(modifiedStart[1])));
    }
    if (!data.length) {
      res.writeHead(400); res.end(JSON.stringify({code:9280,message:"No records found matching the given criteria."})); return;
    }
    if (["Plan_Report", "Mapped_Plans", "All_Registrations"].includes(report) && data.length > 4) {
      if (!req.headers.record_cursor) { res.setHeader("record_cursor", "next-page"); data = data.slice(0,4); }
      else if (req.headers.record_cursor === "next-page") data = data.slice(4);
      else { res.writeHead(400); res.end(JSON.stringify({message:"Bad cursor"})); return; }
    }
    res.end(JSON.stringify({code:3000,data})); return;
  }
  res.writeHead(501); res.end(JSON.stringify({message:"Endpoint not modeled"}));
}).listen(process.env.PORT || 4910);
