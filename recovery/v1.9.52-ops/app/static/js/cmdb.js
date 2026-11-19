(function () {
  const $ = id => document.getElementById(id);
  let catalog = {asset_classes:[], asset_types:[], companies:[], lifecycle_stages:[], operational_statuses:[], environments:[], source_types:[], entitlement_types:[], devices:[], servers:[]};
  let assets = [];
  const selected = new Set();
  const today = () => new Date().toISOString().slice(0,10);
  const value = id => ($(id).value || '').trim();

  function openModal(id) { $(id).classList.add('active'); $(id).setAttribute('aria-hidden','false'); }
  function closeModal(id) { $(id).classList.remove('active'); $(id).setAttribute('aria-hidden','true'); }
  function options(values, selectedValue='') { return values.map(v => `<option value="${escapeAttr(v)}" ${v===selectedValue?'selected':''}>${escapeHtml(v)}</option>`).join(''); }
  function dateText(v) { return v ? new Date(v + 'T00:00:00').toLocaleDateString('zh-CN') : '-'; }
  function expiryClass(row) { return row.expiry_status === 'expired' ? 'expired' : ['urgent','warning'].includes(row.expiry_status) ? 'warning' : ''; }
  function expiryText(row) {
    if (row.days_remaining === null) return '未设置';
    if (row.days_remaining < 0) return `${dateText(row.next_expiry_date)} · 已过期 ${Math.abs(row.days_remaining)} 天`;
    return `${dateText(row.next_expiry_date)} · 剩余 ${row.days_remaining} 天`;
  }

  async function loadCatalog() {
    catalog = await API.get('/api/cmdb/catalog');
    $('cmdb-class-filter').innerHTML = '<option value="">全部大类</option>' + options(catalog.asset_classes);
    $('cmdb-type-filter').innerHTML = '<option value="">全部类型</option>' + options(catalog.asset_types);
    $('cmdb-company-filter').innerHTML = '<option value="">全部公司</option>' + options(catalog.companies);
    $('cmdb-status-filter').innerHTML = '<option value="">全部阶段</option>' + options(catalog.lifecycle_stages);
    $('cmdb-operational-filter').innerHTML = '<option value="">全部状态</option>' + options(catalog.operational_statuses);
    $('cmdb-environment-filter').innerHTML = '<option value="">全部环境</option>' + options(catalog.environments);
    $('cmdb-asset-class').innerHTML = options(catalog.asset_classes, '硬件');
    $('cmdb-asset-type').innerHTML = options(catalog.asset_types);
    $('cmdb-company').innerHTML = '<option value="">未指定</option>' + options(catalog.companies);
    $('cmdb-lifecycle-status').innerHTML = options(catalog.lifecycle_stages, '在用');
    $('cmdb-operational-status').innerHTML = options(catalog.operational_statuses, '运行');
    $('cmdb-environment').innerHTML = options(catalog.environments, '生产');
    $('cmdb-source-of-truth').innerHTML = options(catalog.source_types, '手工维护');
    $('cmdb-event-stage').innerHTML = options(catalog.lifecycle_stages, '在用');
    $('cmdb-batch-status').innerHTML = '<option value="">批量调整阶段…</option>' + options(catalog.lifecycle_stages);
  }

  async function loadSummary() {
    const data = await API.get('/api/cmdb/summary?days=90');
    $('cmdb-total').textContent = data.total;
    $('cmdb-in-service').textContent = data.in_service;
    $('cmdb-expiring').textContent = data.expiring_90;
    $('cmdb-expired').textContent = data.expired;
    $('cmdb-upcoming').innerHTML = data.upcoming.length ? data.upcoming.map(row => `<article class="cmdb-upcoming-item ${expiryClass(row)}"><strong>${escapeHtml(row.name)}</strong><span>${escapeHtml(row.next_expiry_name || '到期')}</span><small>${escapeHtml(expiryText(row))}</small></article>`).join('') : '<div class="empty-state">未来 90 天暂无到期事项</div>';
  }

  async function loadAssets() {
    const params = new URLSearchParams();
    if (value('cmdb-search')) params.set('search', value('cmdb-search'));
    if (value('cmdb-class-filter')) params.set('asset_class', value('cmdb-class-filter'));
    if (value('cmdb-type-filter')) params.set('asset_type', value('cmdb-type-filter'));
    if (value('cmdb-company-filter')) params.set('company', value('cmdb-company-filter'));
    if (value('cmdb-status-filter')) params.set('lifecycle_status', value('cmdb-status-filter'));
    if (value('cmdb-operational-filter')) params.set('operational_status', value('cmdb-operational-filter'));
    if (value('cmdb-environment-filter')) params.set('environment', value('cmdb-environment-filter'));
    if (value('cmdb-expiry-filter')) params.set('expiry', value('cmdb-expiry-filter'));
    assets = await API.get('/api/cmdb/assets?' + params.toString());
    const visible = new Set(assets.map(row => row.id));
    for (const id of selected) if (!visible.has(id)) selected.delete(id);
    renderAssets(); updateSelected();
  }

  function renderAssets() {
    $('cmdb-rows').innerHTML = assets.length ? assets.map(row => {
      const linked = row.linked_device_id ? catalog.devices.find(item=>item.id===row.linked_device_id) : row.linked_server_id ? catalog.servers.find(item=>item.id===row.linked_server_id) : null;
      const link = row.linked_device_id ? `网络设备：${linked?.name||'#'+row.linked_device_id}` : row.linked_server_id ? `服务器/存储：${linked?.name||'#'+row.linked_server_id}` : '未关联';
      const ips = row.ip_addresses.map(ip => `<span class="cmdb-token">${escapeHtml(ip.address)}${ip.is_primary?' · 主':''}</span>`).join('') || '-';
      const serials = row.serial_numbers.map(s => `<span class="cmdb-token serial">${escapeHtml(s.serial_number)}</span>`).join('') || '-';
      return `<tr><td><input class="cmdb-check" type="checkbox" value="${row.id}" ${selected.has(row.id)?'checked':''}></td>
        <td><strong>${escapeHtml(row.name)}</strong><br><small>${escapeHtml(row.asset_class)} / ${escapeHtml(row.asset_type)} · ${escapeHtml([row.brand,row.model].filter(Boolean).join(' ') || '-')}</small><br><small>${escapeHtml(row.company || '未指定公司')}</small></td>
        <td>${ips}</td><td>${serials}</td><td><span class="badge badge-info">${escapeHtml(row.lifecycle_status)}</span> <span class="badge badge-secondary">${escapeHtml(row.operational_status)}</span><br><small>${escapeHtml(row.environment)} · ${escapeHtml(row.criticality)}级</small></td>
        <td class="cmdb-expiry ${expiryClass(row)}">${escapeHtml(expiryText(row))}<br><small>${escapeHtml(row.next_expiry_name || '')}</small></td>
        <td>${escapeHtml(row.department || '-')}<br><small>${escapeHtml(row.owner || '-')} · ${escapeHtml(row.business_service || '未关联业务')}</small></td><td>${escapeHtml(link)}<br><small>${escapeHtml(row.source_of_truth || '-')}</small></td>
        <td><button class="btn btn-sm btn-outline" data-edit="${row.id}">编辑</button> <button class="btn btn-sm btn-outline" data-events="${row.id}">生命周期</button> <button class="btn btn-sm btn-danger" data-delete="${row.id}">删除</button></td></tr>`;
    }).join('') : '<tr><td colspan="9" class="empty-state">暂无匹配的 CMDB 资产</td></tr>';
  }

  function updateSelected() {
    $('cmdb-selected-count').textContent = `已选择 ${selected.size} 项`;
    $('cmdb-select-all').checked = assets.length > 0 && assets.every(row => selected.has(row.id));
    $('cmdb-select-all').indeterminate = selected.size > 0 && !$('cmdb-select-all').checked;
  }

  function addIp(data={}) {
    $('cmdb-ip-list').insertAdjacentHTML('beforeend', `<div class="cmdb-repeat-row ip-row"><label>IP 地址<input class="form-control ip-address" value="${escapeAttr(data.address||'')}" placeholder="10.0.0.1"></label><label>用途<input class="form-control ip-purpose" value="${escapeAttr(data.purpose||'管理')}"></label><label class="check-line"><input class="ip-primary" type="radio" name="cmdb-primary-ip" ${data.is_primary?'checked':''}> 主 IP</label><button class="btn btn-sm btn-danger cmdb-remove" type="button">删除</button></div>`);
  }
  function addSerial(data={}) {
    $('cmdb-serial-list').insertAdjacentHTML('beforeend', `<div class="cmdb-repeat-row serial-row"><label>序列号<input class="form-control serial-number" value="${escapeAttr(data.serial_number||'')}"></label><label>类型<select class="form-control serial-kind">${options(['主机序列号','机框序列号','板卡序列号','电源序列号','授权序列号','其他'],data.kind||'主机序列号')}</select></label><label>标签<input class="form-control serial-label" value="${escapeAttr(data.label||'')}" placeholder="例如：机框A"></label><button class="btn btn-sm btn-danger cmdb-remove" type="button">删除</button></div>`);
  }
  function addEntitlement(data={}) {
    $('cmdb-entitlement-list').insertAdjacentHTML('beforeend', `<div class="cmdb-repeat-row entitlement-row"><label>类型<select class="form-control ent-type">${options(catalog.entitlement_types,data.entitlement_type||'硬件维保')}</select></label><label>名称<input class="form-control ent-name" value="${escapeAttr(data.name||'')}"></label><label>供应商<input class="form-control ent-vendor" value="${escapeAttr(data.vendor||'')}"></label><label>合同号<input class="form-control ent-contract" value="${escapeAttr(data.contract_number||'')}"></label><label>开始<input class="form-control ent-start" type="date" value="${escapeAttr(data.start_date||'')}"></label><label>到期<input class="form-control ent-end" type="date" value="${escapeAttr(data.end_date||'')}"></label><label>提醒天数<input class="form-control ent-reminder" type="number" min="1" max="1095" value="${Number(data.reminder_days||90)}"></label><button class="btn btn-sm btn-danger cmdb-remove" type="button">删除</button></div>`);
  }

  function fillLinkOptions(selectedId='') {
    const kind = value('cmdb-link-kind');
    const rows = kind === 'device' ? catalog.devices : kind === 'server' ? catalog.servers : [];
    $('cmdb-link-id').innerHTML = '<option value="">请选择</option>' + rows.map(row => `<option value="${row.id}" ${String(row.id)===String(selectedId)?'selected':''}>${escapeHtml(row.name)}${row.ips.length?' · '+escapeHtml(row.ips.join(', ')):''}</option>`).join('');
  }

  function resetEditor() {
    $('cmdb-form').reset(); $('cmdb-id').value=''; $('cmdb-form-error').textContent='';
    $('cmdb-asset-class').value='硬件'; $('cmdb-asset-type').value=catalog.asset_types.includes('交换机')?'交换机':catalog.asset_types[0]||''; $('cmdb-company').value=''; $('cmdb-lifecycle-status').value='在用'; $('cmdb-operational-status').value='运行'; $('cmdb-environment').value='生产'; $('cmdb-source-of-truth').value='手工维护'; $('cmdb-criticality').value='一般'; $('cmdb-currency').value='CNY';
    $('cmdb-ip-list').innerHTML=''; $('cmdb-serial-list').innerHTML=''; $('cmdb-entitlement-list').innerHTML=''; fillLinkOptions();
  }

  async function editAsset(id=null) {
    resetEditor();
    if (id) {
      const row = await API.get(`/api/cmdb/assets/${id}`);
      $('cmdb-editor-title').textContent='编辑 CMDB 资产'; $('cmdb-id').value=row.id;
      for (const [field,control] of Object.entries({name:'cmdb-name',asset_class:'cmdb-asset-class',asset_type:'cmdb-asset-type',company:'cmdb-company',brand:'cmdb-brand',model:'cmdb-model',asset_tag:'cmdb-asset-tag',criticality:'cmdb-criticality',lifecycle_status:'cmdb-lifecycle-status',operational_status:'cmdb-operational-status',environment:'cmdb-environment',business_service:'cmdb-business-service',source_of_truth:'cmdb-source-of-truth',last_verified_date:'cmdb-last-verified',purchase_date:'cmdb-purchase-date',initial_expiry_date:'cmdb-initial-expiry',vendor:'cmdb-vendor',purchase_order:'cmdb-purchase-order',purchase_cost:'cmdb-cost',currency:'cmdb-currency',department:'cmdb-department',owner:'cmdb-owner',location:'cmdb-location',notes:'cmdb-notes'})) $(control).value=row[field]??'';
      const kind = row.linked_device_id ? 'device' : row.linked_server_id ? 'server' : '';
      $('cmdb-link-kind').value=kind; fillLinkOptions(row.linked_device_id||row.linked_server_id||'');
      row.ip_addresses.forEach(addIp); row.serial_numbers.forEach(addSerial); row.entitlements.forEach(addEntitlement);
    } else $('cmdb-editor-title').textContent='新增 CMDB 资产';
    openModal('cmdb-editor');
  }

  function importLinked(overwrite=true, quiet=false) {
    const kind=value('cmdb-link-kind'), id=Number(value('cmdb-link-id'));
    const row=(kind==='device'?catalog.devices:catalog.servers).find(item=>item.id===id);
    if(!row){if(!quiet)showToast('请先选择关联设备','warning');return;}
    const assign=(control,incoming)=>{if(incoming&&(overwrite||!value(control)))$(control).value=incoming;};
    assign('cmdb-name',row.name); assign('cmdb-brand',row.brand); assign('cmdb-model',row.model);
    assign('cmdb-asset-tag',row.asset_tag); assign('cmdb-location',row.location); assign('cmdb-owner',row.owner);
    assign('cmdb-company',row.company); if(row.asset_class)$('cmdb-asset-class').value=row.asset_class;
    if(row.asset_type && (overwrite || kind==='server')) $('cmdb-asset-type').value=row.asset_type;
    if(row.operational_status) $('cmdb-operational-status').value=row.operational_status;
    if(row.source_of_truth) $('cmdb-source-of-truth').value=row.source_of_truth;
    if(row.last_verified_date) $('cmdb-last-verified').value=String(row.last_verified_date).slice(0,10);
    const existing=new Set(Array.from(document.querySelectorAll('.ip-address')).map(el=>el.value));
    row.ips.filter(ip=>!existing.has(ip)).forEach((ip,index)=>addIp({address:ip,purpose:'管理',is_primary:!document.querySelector('.ip-primary:checked')&&index===0}));
    (row.serials||[row.serial].filter(Boolean)).filter(serial=>!Array.from(document.querySelectorAll('.serial-number')).some(el=>el.value===serial)).forEach(serial=>addSerial({serial_number:serial,kind:'主机序列号',label:row.name}));
    if(!quiet)showToast(`已带入 ${row.name} 的名称、品牌、型号、IP、序列号和位置资料`,'success');
  }

  function collectRows(selector, mapper) { return Array.from(document.querySelectorAll(selector)).map(mapper).filter(Boolean); }
  function collectPayload() {
    const kind=value('cmdb-link-kind'), link=Number(value('cmdb-link-id'))||null;
    return {name:value('cmdb-name'),asset_class:value('cmdb-asset-class'),asset_type:value('cmdb-asset-type'),company:value('cmdb-company'),brand:value('cmdb-brand'),model:value('cmdb-model'),asset_tag:value('cmdb-asset-tag'),linked_device_id:kind==='device'?link:null,linked_server_id:kind==='server'?link:null,lifecycle_status:value('cmdb-lifecycle-status'),operational_status:value('cmdb-operational-status'),criticality:value('cmdb-criticality'),environment:value('cmdb-environment'),business_service:value('cmdb-business-service'),source_of_truth:value('cmdb-source-of-truth'),last_verified_date:value('cmdb-last-verified')||null,sync_linked_fields:true,purchase_date:value('cmdb-purchase-date')||null,initial_expiry_date:value('cmdb-initial-expiry')||null,vendor:value('cmdb-vendor'),purchase_order:value('cmdb-purchase-order'),purchase_cost:value('cmdb-cost')?Number(value('cmdb-cost')):null,currency:value('cmdb-currency'),department:value('cmdb-department'),owner:value('cmdb-owner'),location:value('cmdb-location'),notes:value('cmdb-notes'),ip_addresses:collectRows('.ip-row',row=>{const address=row.querySelector('.ip-address').value.trim();return address?{address,purpose:row.querySelector('.ip-purpose').value.trim(),is_primary:row.querySelector('.ip-primary').checked}:null;}),serial_numbers:collectRows('.serial-row',row=>{const serial_number=row.querySelector('.serial-number').value.trim();return serial_number?{serial_number,kind:row.querySelector('.serial-kind').value,label:row.querySelector('.serial-label').value.trim()}:null;}),entitlements:collectRows('.entitlement-row',row=>{const name=row.querySelector('.ent-name').value.trim();return name?{entitlement_type:row.querySelector('.ent-type').value,name,vendor:row.querySelector('.ent-vendor').value.trim(),contract_number:row.querySelector('.ent-contract').value.trim(),start_date:row.querySelector('.ent-start').value||null,end_date:row.querySelector('.ent-end').value||null,reminder_days:Number(row.querySelector('.ent-reminder').value||90),notes:''}:null;})};
  }

  function exportAssets(template=false) {
    const ids = !template && selected.size ? `&ids=${encodeURIComponent(Array.from(selected).join(','))}` : '';
    window.location.href = `/api/cmdb/export?template=${template?'true':'false'}${ids}`;
  }

  async function importCsv(event) {
    const file=event.target.files[0]; event.target.value=''; if(!file)return;
    const mode=value('cmdb-import-mode')||'skip';
    if(mode==='overwrite'&&!confirm('覆盖模式会根据关联对象、资产编号、序列号、IP、名称+类型匹配并更新现有记录，是否继续？'))return;
    const form=new FormData(); form.append('file',file);
    showLoading();
    try{
      const response=await fetch(`/api/cmdb/import?mode=${encodeURIComponent(mode)}`,{method:'POST',body:form});
      if(!response.ok)throw new Error(await _errDetail(response));
      const result=await response.json();
      const message=`导入完成：新增 ${result.created}，更新 ${result.updated}，跳过 ${result.skipped}，失败 ${result.error_count}`;
      showToast(message,result.error_count?'warning':'success');
      if(result.errors?.length) alert(message+'\n\n'+result.errors.slice(0,20).map(item=>`第 ${item.line} 行：${item.error}`).join('\n'));
      await Promise.all([loadCatalog(),loadAssets(),loadSummary()]);
    }catch(error){showToast(`CMDB 导入失败：${error.message}`,'error');}finally{hideLoading();}
  }

  async function saveAsset(event) {
    event.preventDefault(); $('cmdb-form-error').textContent=''; $('cmdb-save').disabled=true;
    try { const id=value('cmdb-id'); const payload=collectPayload(); if(id) await API.put(`/api/cmdb/assets/${id}`,payload); else await API.post('/api/cmdb/assets',payload); closeModal('cmdb-editor'); showToast('CMDB 资产已保存','success'); await Promise.all([loadAssets(),loadSummary()]); }
    catch(error){$('cmdb-form-error').textContent=error.message;}
    finally{$('cmdb-save').disabled=false;}
  }

  function openCatalog() {
    $('cmdb-catalog-error').textContent='';
    $('cmdb-catalog-asset-types').value=(catalog.asset_types||[]).join('\n');
    $('cmdb-catalog-entitlement-types').value=(catalog.entitlement_types||[]).join('\n');
    $('cmdb-catalog-companies').value=(catalog.companies||[]).join('\n');
    openModal('cmdb-catalog-modal');
  }

  const catalogLines = id => value(id).split(/\r?\n/).map(item=>item.trim()).filter((item,index,all)=>item&&all.indexOf(item)===index);
  async function saveCatalog(event) {
    event.preventDefault(); $('cmdb-catalog-error').textContent='';
    try {
      await API.put('/api/cmdb/catalog',{asset_types:catalogLines('cmdb-catalog-asset-types'),entitlement_types:catalogLines('cmdb-catalog-entitlement-types'),companies:catalogLines('cmdb-catalog-companies')});
      await loadCatalog(); closeModal('cmdb-catalog-modal'); showToast('CMDB 分类与公司目录已保存','success');
    } catch(error) { $('cmdb-catalog-error').textContent=error.message; }
  }

  async function openTimeline(id) {
    const row=await API.get(`/api/cmdb/assets/${id}`); $('cmdb-timeline-title').textContent=`${row.name} · 生命周期`; $('cmdb-event-asset-id').value=id; $('cmdb-event-stage').value=row.lifecycle_status; $('cmdb-event-date').value=today();
    $('cmdb-event-list').innerHTML=row.lifecycle_events.length?row.lifecycle_events.map(event=>`<article class="cmdb-event"><strong>${escapeHtml(event.stage)}</strong><span>${dateText(event.event_date)}</span><div>${escapeHtml(event.notes||'-')}<br><small>${escapeHtml(event.operator||'系统')}</small></div></article>`).join(''):'<div class="empty-state">暂无生命周期事件</div>'; openModal('cmdb-timeline');
  }

  async function addEvent(event) { event.preventDefault(); const id=value('cmdb-event-asset-id'); await API.post(`/api/cmdb/assets/${id}/events`,{stage:value('cmdb-event-stage'),event_date:value('cmdb-event-date'),operator:value('cmdb-event-operator'),notes:value('cmdb-event-notes'),update_current_status:$('cmdb-event-update-status').checked}); showToast('生命周期记录已添加','success'); await openTimeline(id); await Promise.all([loadAssets(),loadSummary()]); }

  async function deleteOne(id) { const row=assets.find(item=>item.id===id); if(!confirm(`删除 CMDB 资产“${row?.name||id}”及其生命周期记录？不会删除原网络设备或服务器。`))return; await API.delete(`/api/cmdb/assets/${id}`); selected.delete(id); await Promise.all([loadAssets(),loadSummary()]); }
  async function batchDelete(){if(!selected.size){showToast('请先选择资产','warning');return;}if(!confirm(`删除选中的 ${selected.size} 条 CMDB 资产？`))return;await API.post('/api/cmdb/batch-delete',{ids:Array.from(selected)});selected.clear();await Promise.all([loadAssets(),loadSummary()]);}
  async function batchStatus(){const status=value('cmdb-batch-status');if(!selected.size||!status){showToast('请选择资产和生命周期阶段','warning');return;}await API.post('/api/cmdb/batch-status',{ids:Array.from(selected),lifecycle_status:status,operator:'',notes:'批量状态调整'});await Promise.all([loadAssets(),loadSummary()]);showToast('批量状态已更新','success');}

  document.addEventListener('DOMContentLoaded', async () => {
    const initialExpiry = new URLSearchParams(window.location.search).get('expiry');
    if (['expired','30','90','none'].includes(initialExpiry)) $('cmdb-expiry-filter').value = initialExpiry;
    try { await loadCatalog(); await Promise.all([loadAssets(),loadSummary()]); } catch(error){showToast(`CMDB 加载失败：${error.message}`,'error');}
    $('cmdb-add').onclick=()=>editAsset(); $('cmdb-refresh').onclick=()=>Promise.all([loadAssets(),loadSummary()]);
    let searchTimer; $('cmdb-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadAssets,300);};
    ['cmdb-class-filter','cmdb-type-filter','cmdb-company-filter','cmdb-status-filter','cmdb-operational-filter','cmdb-environment-filter','cmdb-expiry-filter'].forEach(id=>$(id).onchange=loadAssets);
    $('cmdb-select-all').onchange=e=>{assets.forEach(row=>e.target.checked?selected.add(row.id):selected.delete(row.id));renderAssets();updateSelected();};
    $('cmdb-rows').onclick=e=>{const check=e.target.closest('.cmdb-check');if(check){check.checked?selected.add(Number(check.value)):selected.delete(Number(check.value));updateSelected();return;}const edit=e.target.closest('[data-edit]'),events=e.target.closest('[data-events]'),del=e.target.closest('[data-delete]');if(edit)editAsset(Number(edit.dataset.edit));if(events)openTimeline(Number(events.dataset.events));if(del)deleteOne(Number(del.dataset.delete));};
    $('cmdb-link-kind').onchange=()=>fillLinkOptions(); $('cmdb-link-id').onchange=()=>importLinked(false,true); $('cmdb-import-linked').onclick=()=>importLinked(true,false);
    $('cmdb-export').onclick=()=>exportAssets(false); $('cmdb-template').onclick=()=>exportAssets(true); $('cmdb-import').onclick=()=>$('cmdb-import-file').click(); $('cmdb-import-file').onchange=importCsv;
    $('cmdb-manage-catalog').onclick=openCatalog; $('cmdb-catalog-form').onsubmit=saveCatalog; document.querySelectorAll('[data-catalog-close]').forEach(el=>el.onclick=()=>closeModal('cmdb-catalog-modal'));
    $('cmdb-add-ip').onclick=()=>addIp(); $('cmdb-add-serial').onclick=()=>addSerial(); $('cmdb-add-entitlement').onclick=()=>addEntitlement();
    document.addEventListener('click',e=>{const remove=e.target.closest('.cmdb-remove');if(remove)remove.parentElement.remove();});
    document.querySelectorAll('[data-cmdb-close]').forEach(el=>el.onclick=()=>closeModal('cmdb-editor')); document.querySelectorAll('[data-timeline-close]').forEach(el=>el.onclick=()=>closeModal('cmdb-timeline'));
    $('cmdb-form').onsubmit=saveAsset; $('cmdb-event-form').onsubmit=addEvent; $('cmdb-batch-delete').onclick=batchDelete; $('cmdb-batch-apply').onclick=batchStatus;
  });
})();
