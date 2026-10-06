"""Auditable segment metrics and opt-in evidence-driven occupancy tiers."""
import math

class ElasticWorkingSet:
    def __init__(self, policy):
        self.policy=policy;self.turn=0;self.last_change=-100;self.pressure=0;self.quiet=0;self.last_fingerprint=None
    def observe(self,fingerprint,current,demand,protected):
        if fingerprint==self.last_fingerprint:return current,None
        self.last_fingerprint=fingerprint;self.turn+=1
        floor=self.policy.get('floor_tokens',12000);normal=self.policy.get('normal_tokens',24000)
        ceiling=self.policy.get('ceiling_tokens',64000)
        levels=sorted(set([floor,normal,self.policy.get('wide_tokens',48000),ceiling]))
        if not 4096<=floor<=normal<=levels[-2]<=ceiling<=65536:raise ValueError('Invalid elastic tiers')
        need=max(demand,protected)
        desired=next((v for v in levels if v>=need),ceiling)
        self.pressure=self.pressure+1 if desired>current else 0
        self.quiet=self.quiet+1 if desired<current else 0
        changed=None
        if self.turn-self.last_change>=self.policy.get('cooldown_turns',4):
            if self.pressure>=self.policy.get('grow_turns',2):
                changed=desired
            elif self.quiet>=self.policy.get('shrink_turns',6):
                changed=max(desired,max((v for v in levels if v<current),default=floor))
        if changed is not None and changed!=current:
            reason='persistent_cluster_demand' if changed>current else 'sustained_locality'
            self.last_change=self.turn;self.pressure=self.quiet=0
            return changed,{'from_tokens':current,'to_tokens':changed,'reason':reason,'demand_tokens':need,'turn':self.turn}
        return current,None


def segment_metrics(groups, ledger, weights_by_source, prior=None, extra_units=None,
                    raw_start=None, anchor_ids=()):
    represented={r['source_id'] for r in ledger if r.get('source_id')}
    raw={r['source_id'] for r in ledger if r.get('source_id') and r['representation']=='RAW'}
    sizes=[];splits=0;units=set()
    remaining_anchors=set(anchor_ids)
    for g in groups:
        members=set(g['source_ids']);hits=members&raw
        if raw_start is not None and g['end']<=raw_start:
            # Content hashes can recur in old acknowledgements. An old occurrence
            # is not selected merely because an identical recent row is selected.
            hits &= remaining_anchors
            remaining_anchors -= hits
        if hits:
            sizes.append(sum(weights_by_source.get(h,0) for h in g['source_ids'] if h in hits));units.add(g['segment_id'])
            splits+=int(hits!=members)
    # Quoted recall/compact blocks count as coherent representations, separately
    # from RAW coverage. Storage pages are deliberately not prompt segments.
    others={r['page_id'] for r in ledger if r.get('reason') in ('recall','warm_block','rehydrated_read')}
    units|=others
    raw_sizes=list(sizes)
    sizes += [u['tokens'] for u in (extra_units or []) if u['id'] in others]
    total=sum(sizes);ordered=sorted(sizes)
    def q(p):return ordered[min(len(ordered)-1,int(p*(len(ordered)-1)))] if ordered else 0
    prev=prior or {'sources':set(),'units':set()}
    old=prev['sources'];retained=represented&old;removed=old-represented;added=represented-old
    old_total=sum(weights_by_source.get(h,0) for h in old)
    removed_n=sum(weights_by_source.get(h,0) for h in removed)
    overlap=len(retained)/max(1,len(represented|old)) if old else 1
    small={n:sum(x for x in sizes if x<n)/max(1,total) for n in (1000,2000,4000)}
    duplicates=len(ledger)-len({(r['page_id'],r.get('source_id'),r['representation']) for r in ledger})
    duplicate_rate=duplicates/max(1,len(ledger))
    split_rate=splits/max(1,len(raw_sizes))
    score=100*(.30*small[2000]+.25*(1-overlap)+.20*removed_n/max(1,old_total)+.15*split_rate+.10*duplicate_rate)
    fields={'selected_segments':len(units),'raw_segment_count':len(raw_sizes),'measured_display_segments':len(sizes),'segment_mean_tokens':total/max(1,len(sizes)),'segment_median_tokens':q(.5),'segment_p10_tokens':q(.1),'segment_p90_tokens':q(.9),'segment_min_tokens':q(0),'fraction_active_in_segments_under_1k':small[1000],'fraction_active_in_segments_under_2k':small[2000],'fraction_active_in_segments_under_4k':small[4000],'fraction_raw_in_segments_under_1k':sum(n for n in raw_sizes if n<1000)/max(1,sum(raw_sizes)),'fraction_raw_in_segments_under_2k':sum(n for n in raw_sizes if n<2000)/max(1,sum(raw_sizes)),'fraction_raw_in_segments_under_4k':sum(n for n in raw_sizes if n<4000)/max(1,sum(raw_sizes)),'segments_replaced':len(prev['units']-units),'sources_jaccard':overlap,'retained_source_tokens':sum(weights_by_source.get(h,0) for h in retained),'new_source_tokens':sum(weights_by_source.get(h,0) for h in added),'removed_source_tokens':removed_n,'source_token_retained_fraction':1-removed_n/max(1,old_total) if old else 1,'duplicate_ledger_rows':duplicates,'partial_raw_clusters':splits,'raw_cluster_split_rate':split_rate,'fragmentation_score':score,'recall_segment_count':len(others)}
    return fields,{'sources':represented,'units':units}
