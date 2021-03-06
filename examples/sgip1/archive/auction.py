import sys
import fncs
import json
from tesp_support import hvac
from tesp_support import simple_auction
from tesp_support import parse_fncs_magnitude
from tesp_support import parse_kw

# these should be in a configuration file as well; TODO synch the proper hour of day
bWantMarket = True
if len(sys.argv) > 3:
    if sys.argv[3] == 'NoMarket':
        bWantMarket = False
        print ('Disabled the market')
time_stop = int (48 * 3600) # simulation time in seconds
StartTime = '2013-07-01 00:00:00 PST'

# ====== load the JSON dictionary; create the corresponding objects =========

filename = sys.argv[1]
lp = open(filename).read()
dict = json.loads(lp)

market_key = list(dict['markets'].keys())[0]  # TODO: only using the first market
market_row = dict['markets'][market_key]
unit = market_row['unit']

auction_meta = {'clearing_price':{'units':'USD','index':0},'clearing_type':{'units':'[0..5]=[Null,Fail,Price,Exact,Seller,Buyer]','index':1}}
controller_meta = {'bid_price':{'units':'USD','index':0},'bid_quantity':{'units':unit,'index':1}}
auction_metrics = {'Metadata':auction_meta,'StartTime':StartTime}
controller_metrics = {'Metadata':controller_meta,'StartTime':StartTime}

aucObj = simple_auction (market_row, market_key)

dt = float(dict['dt'])
period = aucObj.period

topicMap = {} # to dispatch incoming FNCS messages; 0..5 for LMP, Feeder load, airtemp, mtr volts, hvac load, hvac state
topicMap['LMP'] = [aucObj, 0]
topicMap['refload'] = [aucObj, 1]

hvacObjs = {}
hvac_keys = list(dict['controllers'].keys())
for key in hvac_keys:
  row = dict['controllers'][key]
  hvacObjs[key] = hvac (row, key, aucObj)
  ctl = hvacObjs[key]
  topicMap[key + '#Tair'] = [ctl, 2]
  topicMap[key + '#V1'] = [ctl, 3]
  topicMap[key + '#Load'] = [ctl, 4]
  topicMap[key + '#On'] = [ctl, 5]

# ==================== Time step looping under FNCS ===========================

fncs.initialize()
aucObj.initAuction()
LMP = aucObj.mean
refload = 0.0
bSetDefaults = True

tnext_bid = period - 2 * dt  #3 * dt  # controllers calculate their final bids
tnext_agg = period - 2 * dt  # auction calculates and publishes aggregate bid
tnext_opf = period - 1 * dt  # PYPOWER executes OPF and publishes LMP (no action here)
tnext_clear = period         # clear the market with LMP
tnext_adjust = period        # + dt   # controllers adjust setpoints based on their bid and clearing

time_granted = 0
while (time_granted < time_stop):
    time_granted = fncs.time_request(time_stop)
    hour_of_day = 24.0 * ((float(time_granted) / 86400.0) % 1.0)

    # update the data from FNCS messages
    events = fncs.get_events()
    for key in events:
        topic = key.decode()
        value = fncs.get_value(key).decode()
        row = topicMap[topic]
        if row[1] == 0:
            LMP = parse_fncs_magnitude (value)
            aucObj.set_lmp (LMP)
        elif row[1] == 1:
            refload = parse_kw (value)
            aucObj.set_refload (refload)
        elif row[1] == 2:
            row[0].set_air_temp (value)
        elif row[1] == 3:
            row[0].set_voltage (value)
        elif row[1] == 4:
            row[0].set_hvac_load (value)
        elif row[1] == 5:
            row[0].set_hvac_state (value)

    # set the time-of-day schedule
    for key, obj in hvacObjs.items():
        if obj.change_basepoint (hour_of_day):
            fncs.publish (obj.name + '/cooling_setpoint', obj.basepoint)
    if bSetDefaults:
        for key, obj in hvacObjs.items():
            fncs.publish (obj.name + '/bill_mode', 'HOURLY')
            fncs.publish (obj.name + '/monthly_fee', 0.0)
            fncs.publish (obj.name + '/thermostat_deadband', obj.deadband)
        bSetDefaults = False

    if time_granted >= tnext_bid:
        print ('**', tnext_clear)
        aucObj.clear_bids()
        time_key = str (int (tnext_clear))
        controller_metrics [time_key] = {}
        for key, obj in hvacObjs.items():
            bid = obj.formulate_bid () # bid is [price, quantity, on_state]
            if bWantMarket:
                aucObj.collect_bid (bid)
            controller_metrics[time_key][obj.name] = [bid[0], bid[1]]
        tnext_bid += period

    if time_granted >= tnext_agg:
        aucObj.aggregate_bids()
        fncs.publish ('unresponsive_mw', aucObj.agg_unresp)
        fncs.publish ('responsive_max_mw', aucObj.agg_resp_max)
        fncs.publish ('responsive_c2', aucObj.agg_c2)
        fncs.publish ('responsive_c1', aucObj.agg_c1)
        fncs.publish ('responsive_deg', aucObj.agg_deg)
        tnext_agg += period

    if time_granted >= tnext_clear:
        if bWantMarket:
            aucObj.clear_market()
            fncs.publish ('clear_price', aucObj.clearing_price)
            for key, obj in hvacObjs.items():
                obj.inform_bid (aucObj.clearing_price)
        time_key = str (int (tnext_clear))
        auction_metrics [time_key] = {aucObj.name:[aucObj.clearing_price, aucObj.clearing_type]}
        tnext_clear += period

    if time_granted >= tnext_adjust:
        if bWantMarket:
            for key, obj in hvacObjs.items():
                fncs.publish (obj.name + '/price', aucObj.clearing_price)
                if obj.bid_accepted ():
                    fncs.publish (obj.name + '/cooling_setpoint', obj.setpoint)
        tnext_adjust += period

# ==================== Finalize the metrics output ===========================

print ('writing metrics', flush=True)
auction_op = open ('auction_' + sys.argv[2] + '_metrics.json', 'w')
controller_op = open ('controller_' + sys.argv[2] + '_metrics.json', 'w')
print (json.dumps(auction_metrics), file=auction_op)
print (json.dumps(controller_metrics), file=controller_op)
auction_op.close()
controller_op.close()

print ('finalizing FNCS', flush=True)
fncs.finalize()

