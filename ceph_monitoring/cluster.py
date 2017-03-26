import re
import json
import logging
import itertools
import collections

from ipaddr import IPNetwork
import numpy

from cephlib.crush import load_crushmap
from cephlib.common import AttredDict


from .hw_info import get_hw_info, ssize2b


logger = logging.getLogger("cephlib.parse")
NO_VALUE = -1


class CephOSD(object):
    def __init__(self):
        self.id = None
        self.reweight = None
        self.status = None
        self.host = None
        self.cluster_ip = None
        self.public_ip = None
        self.pg_count = None
        self.config = None
        self.pgs = {}
        self.data_stor_stats = None
        self.j_stor_stats = None
        self.version = None
        self.osd_perf = {}  # map perf field (apply_latency/journal_latency/etc) to either numpy.array with per sec
                            # avg values (-1 mean no value available), or single value for whole range
        self.historic_ops_storage_path = None

        self.used_space = None
        self.free_space = None
        self.free_perc = None
        self.procinfo = None
        self.cmdline = None

    @property
    def daemon_runs(self):
        return self.config is not None


class CephMonitor(object):
    def __init__(self):
        self.name = None
        self.status = None
        self.host = None
        self.role = None


class Pool(object):
    def __init__(self):
        self.id = None
        self.name = None


class NetLoad(object):
    def __init__(self):
        self.send_bytes = None
        self.recv_bytes = None
        self.send_packets = None
        self.recv_packets = None
        self.send_bytes_avg = None
        self.recv_bytes_avg = None
        self.send_packets_avg = None
        self.recv_packets_avg = None


class NetworkAdapter(object):
    def __init__(self, name):
        self.name = name
        self.ips = []
        self.is_phy = None
        self.speed = None
        self.duplex = None
        self.load = None


class DiskLoad(object):
    def __init__(self):
        self.read_bytes = None
        self.write_bytes = None
        self.read_iops = None
        self.write_iops = None
        self.io_time = None
        self.w_io_time = None
        self.iops = None
        self.queue_depth = None
        self.lat = None


class Disk(object):
    def __init__(self, dev, load):
        self.dev = dev
        self.load = load


class Host(object):
    def __init__(self, name, stor_id):
        self.name = name
        self.net_adapters = {}
        self.disks = {}
        self.uptime = None
        self.perf_monitoring = None
        self.stor_id = stor_id
        self.ceph_cluster_adapter = None
        self.ceph_public_adapter = None
        self.osd_ids = set()
        self.mon_name = None
        self.open_tcp_sock = None
        self.open_udp_sock = None


class TabulaRasa(object):
    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def get(self, name, default=None):
        try:
            return self.__dict__.get(name, default)
        except KeyError:
            raise AttributeError(name)

    def __contains__(self, name):
        return name in self.__dict__


dev_re = re.compile(r"/dev/([^0-9]*)\d*")
def dev_name(path):
    rr = dev_re.match(path)
    if rr:
        return rr.group(1)
    raise ValueError("Can't find source dev name in {0!r} (must be /dev/XXXX\\d*)".format(path))


netstat_fields = "recv_bytes recv_packets rerrs rdrop rfifo rframe rcompressed" + \
                 " rmulticast send_bytes send_packets serrs sdrop sfifo scolls" + \
                 " scarrier scompressed"

NetStats = collections.namedtuple("NetStats", netstat_fields)


def parse_netdev(netdev):
    info = {}
    for line in netdev.strip().split("\n")[2:]:
        adapter, data = line.split(":")
        adapter = adapter.strip()
        assert adapter not in info
        info[adapter] = NetStats(*map(int, data.split()))

    return info


def parse_meminfo(meminfo):
    info = {}
    for line in meminfo.split("\n"):
        line = line.strip()
        if line == '':
            continue
        name, data = line.split(":", 1)
        data = data.strip()
        if " " in data:
            data = data.replace(" ", "")
            assert data[-1] == 'B'
            val = ssize2b(data[:-1])
        else:
            val = int(data)
        info[name] = val
    return info


def find(lst, check, default=None):
    for obj in lst:
        if check(obj):
            return obj
    return default


def parse_txt_ceph_config(data):
    config = {}
    for line in data.strip().split("\n"):
        name, val = line.split("=", 1)
        config[name.strip()] = val.strip()
    return config


US2S = 0.000001
MS2S = 0.001


class CephCluster(object):
    # incorporated both ceph cluster and all nodes information
    def __init__(self, storage):
        # servers
        self.hosts = {}
        self.storage = storage
        self.report_collected_at_local = None
        self.report_collected_at_gmt = None
        self.perf_data = None

        # ceph cluster
        self.osds = []
        self.mons = []
        self.pools = {}
        self.osd_map = {}  # map osd id to osd object
        self.crush = None
        self.cluster_net = None
        self.public_net = None
        self.settings = None
        self.overall_status = None
        self.health_summary = None
        self.num_pgs = None
        self.bytes_used = None
        self.bytes_total = None
        self.bytes_avail = None
        self.data_bytes = None
        self.write_bytes_sec = None
        self.op_per_sec = None
        self.pgmap_stat = None
        self.monmap_stat = None

        # synthetic props
        self.usage = None

    def load(self):
        self.settings = AttredDict(**parse_txt_ceph_config(self.storage.txt.master.default_config))
        self.cluster_net = IPNetwork(self.settings['cluster_network'])
        self.public_net = IPNetwork(self.settings['public_network'])
        self.crush = load_crushmap(content=self.storage.txt.master.crushmap)

        self.perf_data, self.osd_perf_dump, self.osd_historys_ops_paths = self.get_perf_monitoring()
        self.load_ceph_settings()
        self.load_hosts()
        # TODO: set reweight for OSD
        self.load_PG_distribution()
        self.load_osds()
        self.load_pools()
        self.load_monitors()

        coll_time = self.storage.txt['master/collected_at'].strip()
        self.report_collected_at_local, self.report_collected_at_gmt, _ = coll_time.split("\n")

    def get_perf_monitoring(self):
        all_data = {}
        osd_perf_dump = {}
        osd_historys_ops_paths = {}
        osd_rr = re.compile(r"osd(\d+)$")
        for is_file, host_id in self.storage.txt.perf_monitoring:
            if is_file:
                logger.warning("Unexpected file %r in perf_monitoring folder", host_id)

            host_data = all_data[host_id] = {}
            for is_file, fname in self.storage.txt.perf_monitoring[host_id]:
                if is_file and fname == 'collected_at.arr':
                    path = "perf_monitoring/{0}/collected_at.arr".format(host_id, fname)
                    host_data['collected_at'] = numpy.array(self.storage.raw.get_array(path))
                    continue

                if is_file and fname.count('.') == 3:
                    sensor, dev, metric, ext = fname.split(".")
                    if ext == 'arr':
                        path = "perf_monitoring/{0}/{1}".format(host_id, fname)
                        host_data.setdefault(sensor, {}).setdefault(dev, {})[metric] = \
                            numpy.array(self.storage.raw.get_array(path))
                        continue
                    elif ext == 'json' and sensor == 'ceph' and metric == 'perf_dump':
                        os_id = osd_rr.match(dev)
                        assert os_id, "{0!r} don't match osdXXX name".format(dev)
                        assert os_id.group(1) not in osd_perf_dump, "Two set of perf_dump data for osd {0}"\
                            .format(os_id.group(1))
                        path = "perf_monitoring/{0}/{1}".format(host_id, fname)
                        osd_perf_dump[int(os_id.group(1))] = json.loads(self.storage.raw.get(path))
                        continue
                    elif ext == 'bin' and sensor == 'ceph' and metric == 'historic':
                        os_id = osd_rr.match(dev)
                        assert os_id, "{0!r} don't match osdXXX name".format(dev)
                        assert os_id.group(1) not in osd_perf_dump, \
                            "Two set of osd_historys_ops_paths data for osd {0}".format(os_id.group(1))
                        osd_historys_ops_paths[int(os_id.group(1))] = "perf_monitoring/{0}/{1}".format(host_id, fname)
                        continue
                    elif ext == 'json' and sensor == 'ceph' and metric == 'historic_js':
                        continue

                logger.warning("Unexpected %s %r in %r host performance_data folder",
                               'file' if is_file else 'folder', fname, host_id)

        return all_data, osd_perf_dump, osd_historys_ops_paths

    def load_ceph_settings(self):
        mstorage = self.storage.json.master
        self.overall_status = mstorage.status['health']['overall_status']
        self.health_summary = mstorage.status['health']['summary']
        self.num_pgs = mstorage.status['pgmap']['num_pgs']
        self.bytes_used = mstorage.status['pgmap']["bytes_used"]
        self.bytes_total = mstorage.status['pgmap']["bytes_total"]
        self.bytes_avail = mstorage.status['pgmap']["bytes_avail"]
        self.data_bytes = mstorage.status['pgmap']["data_bytes"]
        self.write_bytes_sec = mstorage.status['pgmap'].get("write_bytes_sec", 0)
        self.op_per_sec = mstorage.status['pgmap'].get("op_per_sec", 0)
        self.pgmap_stat = mstorage.status['pgmap']
        self.monmap_stat = mstorage.status['monmap']

    def load_hosts(self):
        hosts = self.storage.txt.hosts
        tcp_sock_re = re.compile('(?im)^tcp6?\\b')
        udp_sock_re = re.compile('(?im)^udp6?\\b')

        for is_file, host_ip_name in hosts:
            assert not is_file
            ip, host_name = host_ip_name.split("-", 1)

            stor_node = hosts[host_ip_name]
            jstor_node = self.storage.json.hosts[host_ip_name]
            host = Host(host_name, stor_id=host_ip_name)

            self.hosts[host.name] = host

            lshw_xml = stor_node.get('lshw', ext='xml')

            if lshw_xml is None:
                host.hw_info = None
            else:
                try:
                    host.hw_info = get_hw_info(lshw_xml)
                except:
                    host.hw_info = None

            info = parse_meminfo(stor_node.meminfo)
            host.mem_total = info['MemTotal']
            host.mem_free = info['MemFree']
            host.swap_total = info['SwapTotal']
            host.swap_free = info['SwapFree']

            loadavg = stor_node.get('loadavg')
            host.load_5m = None if loadavg is None else float(loadavg.strip().split()[1])
            host.open_tcp_sock = len(tcp_sock_re.findall(stor_node.netstat))
            host.open_udp_sock = len(udp_sock_re.findall(stor_node.netstat))

            # net_stats = parse_netdev(stor_node.netdev)
            host.uptime = float(stor_node.uptime.split()[0])
            host.perf_monitoring = self.perf_data.get(host.stor_id)

            if host.perf_monitoring:
                dtime = (host.perf_monitoring['collected_at'][-2] - host.perf_monitoring['collected_at'][0]) * US2S
                self.fill_io_devices_usage_stats(host)
                for name, adapter_dct in jstor_node.interfaces.items():
                    adapter_dct = adapter_dct.copy()
                    dev = adapter_dct.pop('dev')
                    adapter = NetworkAdapter(dev)
                    adapter.__dict__.update(adapter_dct)
                    host.net_adapters[dev] = adapter

                    adapter.load = None
                    if dtime and dtime > 1.0:
                        load = NetLoad()
                        load_node = host.perf_monitoring.get('net-io', {}).get(adapter.name, {})
                        for metric in 'send_bytes send_packets recv_packets recv_bytes'.split():
                            data = load_node.get(metric)
                            if data is None:
                                break
                            setattr(load, metric, data)
                            setattr(load, metric + "_avg", sum(data) / dtime)
                        else:
                            adapter.load = load

            ip_rr_s = r"\d+:\s+(?P<adapter>.*?)\s+inet\s+(?P<ip>\d+\.\d+\.\d+\.\d+)/(?P<size>\d+)"
            for line in stor_node.ipa.split("\n"):
                match = re.match(ip_rr_s, line)
                if match is not None:
                    net_addr = IPNetwork(match.group('ip'), int(match.group('size')))
                    adapter = host.net_adapters[match.group('adapter')]
                    adapter.ips.append(net_addr)

                    if net_addr.ip in self.cluster_net:
                        host.ceph_cluster_adapter = adapter

                    if net_addr.ip in self.public_net:
                        host.ceph_public_adapter = adapter

    def fill_io_devices_usage_stats(self, host):
        if 'block-io' not in host.perf_monitoring:
            return

        cl_at = host.perf_monitoring['collected_at']
        dtime = (cl_at[-2] - cl_at[0]) * US2S
        io_data = host.perf_monitoring['block-io']
        for dev, data in io_data.items():
            if dtime > 1.0:
                load = DiskLoad()
                load.read_bytes = sum(data['sectors_read']) / dtime
                load.write_bytes = sum(data['sectors_written']) / dtime
                load.read_iops = sum(data['reads_completed']) / dtime
                load.write_iops = sum(data['writes_completed']) / dtime
                load.io_time = MS2S * sum(data['io_time']) / dtime
                load.w_io_time = MS2S * sum(data['weighted_io_time']) / dtime
                load.iops = load.read_iops + load.write_iops
                load.queue_depth = load.w_io_time
                load.lat = load.w_io_time / load.iops if load.iops > 1E-5 else None
            else:
                load = None

            host.disks[dev] = Disk(dev, load)

    def load_PG_distribution(self):
        try:
            pg_dump = self.storage.json.master.pg_dump
        except AttributeError:
            pg_dump = None

        self.osd_pool_pg_2d = collections.defaultdict(lambda: collections.Counter())
        self.sum_per_pool = collections.Counter()
        self.sum_per_osd = collections.Counter()
        pool_id2name = dict((dt['poolnum'], dt['poolname'])
                            for dt in self.storage.json.master.osd_lspools)

        if pg_dump is None:
            for is_file, node in self.storage.txt.osd:
                if not is_file and node.isdigit():
                    osd_num = int(node)
                    for pg in node.pgs.split():
                        pool_id, _ = pg.split(".")
                        pool_name = pool_id2name[int(pool_id)]
                        self.osd_pool_pg_2d[osd_num][pool_name] += 1
                        self.sum_per_pool[pool_name] += 1
                        self.sum_per_osd[osd_num] += 1
        else:
            for pg in pg_dump['pg_stats']:
                pool = int(pg['pgid'].split('.', 1)[0])
                for osd_num in pg['acting']:
                    pool_name = pool_id2name[pool]
                    self.osd_pool_pg_2d[osd_num][pool_name] += 1
                    self.sum_per_pool[pool_name] += 1
                    self.sum_per_osd[osd_num] += 1

    def load_osds(self):
        ip2host = {}

        for host in self.hosts.values():
            for ip_net in host.net_adapters.values():
                for net_addr in ip_net.ips:
                    ip2host[str(net_addr.ip)] = host

        osd_rw_dict = dict((node['id'], node['reweight'])
                           for node in self.storage.json.master.osd_tree['nodes']
                           if node['id'] >= 0)

        osd_versions = {}
        version_rr = re.compile(r'osd.(?P<osd_id>\d+)\s*:\s*' +
                                r'\{"version":"ceph version\s+(?P<version>[^ ]*)\s+\((?P<hash>[^)]*?)\)"\}\s*$')
        for line in self.storage.txt.master.osd_versions.split("\n"):
            rr = version_rr.match(line)
            if rr:
                osd_versions[int(rr.group('osd_id'))] = (rr.group('version'), rr.group('hash'))

        osd_perf_scalar = {}
        for node in self.storage.json.master.osd_perf['osd_perf_infos']:
            osd_perf_scalar[node['id']] = {"apply_latency": node["perf_stats"]["apply_latency_ms"],
                                           "commitcycle_latency": node["perf_stats"]["commit_latency_ms"]}

        for osd_data in self.storage.json.master.osd_dump['osds']:
            osd = CephOSD()
            self.osds.append(osd)
            self.osd_map[osd.id] = osd

            osd.cluster_ip = osd_data['cluster_addr'].split(":", 1)[0]
            osd.public_ip = osd_data['public_addr'].split(":", 1)[0]
            osd.id = osd_data['osd']
            osd.reweight = osd_rw_dict[osd.id]
            osd.status = 'up' if osd_data['up'] else 'down'
            osd.version = osd_versions[osd.id]

            try:
                osd.host = ip2host[osd.cluster_ip]
            except KeyError:
                logger.exception("Can't found host for osd %s, as no host own %r ip addr",
                                 osd.id, osd.cluster_ip)
                raise

            osd.host.osd_ids.add(osd.id)

            try:
                osd_stor_node = self.storage.json.osd[str(osd.id)]
                osd.data_stor_stats = TabulaRasa(**osd_stor_node.data.stats)
                osd.j_stor_stats = TabulaRasa(**osd_stor_node.journal.stats)
                osd.data_stor_stats.src_dev = dev_name(osd.data_stor_stats.dev)
                osd.data_stor_stats.load = osd.host.disks[osd.data_stor_stats.src_dev].load
                osd.j_stor_stats.src_dev = dev_name(osd.j_stor_stats.dev)
                osd.j_stor_stats.load = osd.host.disks[osd.j_stor_stats.src_dev].load

                osd.used_space = osd.data_stor_stats.get('used')
                osd.free_space = osd.data_stor_stats.get('avail')
                osd.free_perc = int((osd.free_space * 100.0) / (osd.free_space + osd.used_space) + 0.5)

            except (AttributeError, KeyError):
                osd.data_stor_stats = None
                osd.j_stor_stats = None

            if osd.id in self.osd_perf_dump:
                fstor = [obj["filestore"] for obj in self.osd_perf_dump[osd.id]]
                for field in ("apply_latency", "commitcycle_latency", "journal_latency"):
                    data = [obj[field]["avgcount"] for obj in fstor]
                    count = numpy.array(data, dtype=numpy.float32)
                    values = numpy.array([obj["filestore"][field]["sum"] for obj in self.osd_perf_dump[osd.id]],
                                          dtype=numpy.float32)

                    with numpy.errstate(divide='ignore', invalid='ignore'):
                        avg_vals = (values[1:] - values[:-1]) / (count[1:] - count[:-1])

                    avg_vals[avg_vals == numpy.inf] = NO_VALUE
                    avg_vals[numpy.isnan(avg_vals)] = NO_VALUE
                    osd.osd_perf[field] = avg_vals

                data = [obj['journal_wr_bytes']["avgcount"] for obj in fstor]
                arr = numpy.array(data, dtype=numpy.float32)
                osd.osd_perf["journal_ops"] = arr[1:] - arr[:-1]

                data = [obj['journal_wr_bytes']["sum"] for obj in fstor]
                arr = numpy.array(data, dtype=numpy.float32)
                osd.osd_perf["journal_bytes"] = arr[1:] - arr[:-1]
            else:
                try:
                    osd.osd_perf = osd_perf_scalar[osd.id]
                except KeyError:
                    msg = "Can't found perf_stats for osd {0} in master/osd_perf[osd_perf_infos]".format(osd.id)
                    logger.error(msg)
                    raise KeyError(msg)

            osd.historic_ops_storage_path = self.osd_historys_ops_paths.get(osd.id)
            osd.pg_count = None if self.sum_per_osd is None else self.sum_per_osd[osd.id]
            config = self.storage.txt.osd[str(osd.id)].config
            osd.config = None if config is None else AttredDict(**parse_txt_ceph_config(config))

            try:
                osd.cmdline = self.storage.raw.get('osd/{0}/cmdline'.format(osd.id)).split("\x00")
            except KeyError:
                pass

            try:
                osd.procinfo = self.storage.json.osd[str(osd.id)].procinfo
            except (KeyError, AttributeError):
                pass

        self.osds.sort(key=lambda x: x.id)

    def load_pools(self):
        self.pools = {}

        for pool_part in self.storage.json.master.osd_dump['pools']:
            pool = Pool()
            pool.id = pool_part['pool']
            pool.name = pool_part['pool_name']
            pool.__dict__.update(pool_part)
            self.pools[int(pool.id)] = pool

        for pool_part in self.storage.json.master.rados_df['pools']:
            if 'categories' not in pool_part:
                self.pools[int(pool_part['id'])].__dict__.update(pool_part)
            else:
                assert len(pool_part['categories']) == 1
                cat = pool_part['categories'][0].copy()
                del cat['name']
                self.pools[int(pool_part['id'])].__dict__.update(cat)

    def load_monitors(self):
        srv_health = self.storage.json.master.status['health']['health']['health_services']
        assert len(srv_health) == 1
        for srv in srv_health[0]['mons']:
            mon = CephMonitor()
            mon.health = srv["health"]
            mon.name = srv["name"]
            mon.host = srv["name"]

            for host in self.hosts.values():
                if host.name == mon.host:
                    host.mon_name = mon.name
                    break
            else:
                msg = "Can't found host for monitor {0!r}".format(mon.name)
                logger.error(msg)
                raise ValueError(msg)

            mon.kb_avail = srv["kb_avail"]
            mon.avail_percent = srv["avail_percent"]
            self.mons.append(mon)

