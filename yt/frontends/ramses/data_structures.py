"""
RAMSES-specific data structures



"""
# BytesIO needs absolute import
from __future__ import print_function, absolute_import

#-----------------------------------------------------------------------------
# Copyright (c) 2013, yt Development Team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------

import os
import numpy as np
import stat
import weakref

from yt.extern.six import string_types
from yt.funcs import \
    mylog, \
    setdefaultattr
from yt.geometry.oct_geometry_handler import \
    OctreeIndex
from yt.geometry.geometry_handler import \
    YTDataChunk
from yt.data_objects.static_output import \
    Dataset
from yt.data_objects.octree_subset import \
    OctreeSubset
from yt.data_objects.particle_filters import add_particle_filter

from yt.utilities.physical_constants import mp, kb
from .definitions import ramses_header, field_aliases, particle_families
from .fields import \
    RAMSESFieldInfo, _X
from .hilbert import get_cpu_list
from .particle_handlers import get_particle_handlers
from .field_handlers import get_field_handlers
import yt.utilities.fortran_utils as fpu
from yt.geometry.oct_container import \
    RAMSESOctreeContainer
from yt.arraytypes import blankRecordArray

from yt.utilities.lib.cosmology_time import \
    friedman


class RAMSESDomainFile(object):
    _last_mask = None
    _last_selector_id = None

    def __init__(self, ds, domain_id):
        self.ds = ds
        self.domain_id = domain_id

        num = os.path.basename(ds.parameter_filename).split("."
                )[0].split("_")[1]
        basedir = os.path.abspath(
            os.path.dirname(ds.parameter_filename))
        basename = "%s/%%s_%s.out%05i" % (
            basedir, num, domain_id)
        part_file_descriptor = "%s/part_file_descriptor.txt" % basedir
        for t in ['grav', 'amr']:
            setattr(self, "%s_fn" % t, basename % t)
        self._part_file_descriptor = part_file_descriptor
        self._read_amr_header()
        # self._read_hydro_header()

        # Autodetect field files
        field_handlers = [FH(self)
                          for FH in get_field_handlers()
                          if FH.any_exist(ds)]
        self.field_handlers = field_handlers
        for fh in field_handlers:
            mylog.debug('Detected particle type %s in domain_id=%s' % (fh.ftype, domain_id))
            fh.detect_fields(ds)
            # self._add_ftype(fh.ftype)

        # Autodetect particle files
        particle_handlers = [PH(ds, domain_id)
                             for PH in get_particle_handlers()
                             if PH.any_exist(ds)]
        self.particle_handlers = particle_handlers
        for ph in particle_handlers:
            mylog.debug('Detected particle type %s in domain_id=%s' % (ph.ptype, domain_id))
            ph.read_header()
            # self._add_ptype(ph.ptype)

        # Load the AMR structure
        self._read_amr()

    _hydro_offset = None
    _level_count = None

    def __repr__(self):
        return "RAMSESDomainFile: %i" % self.domain_id

    @property
    def level_count(self):
        lvl_count = None
        for fh in self.field_handlers:
            fh.offset
            if lvl_count is None:
                lvl_count = fh.level_count.copy()
            else:
                lvl_count += fh._level_count
        return lvl_count

    def _read_amr_header(self):
        hvals = {}
        f = open(self.amr_fn, "rb")
        for header in ramses_header(hvals):
            hvals.update(fpu.read_attrs(f, header))
        # That's the header, now we skip a few.
        hvals['numbl'] = np.array(hvals['numbl']).reshape(
            (hvals['nlevelmax'], hvals['ncpu']))
        fpu.skip(f)
        if hvals['nboundary'] > 0:
            fpu.skip(f, 2)
            self.ngridbound = fpu.read_vector(f, 'i').astype("int64")
        else:
            self.ngridbound = np.zeros(hvals['nlevelmax'], dtype='int64')
        free_mem = fpu.read_attrs(f, (('free_mem', 5, 'i'), ) )  # NOQA
        ordering = fpu.read_vector(f, 'c')  # NOQA
        fpu.skip(f, 4)
        # Now we're at the tree itself
        # Now we iterate over each level and each CPU.
        self.amr_header = hvals
        self.amr_offset = f.tell()
        self.local_oct_count = hvals['numbl'][self.ds.min_level:, self.domain_id - 1].sum()
        self.total_oct_count = hvals['numbl'][self.ds.min_level:,:].sum(axis=0)

    def _read_amr(self):
        """Open the oct file, read in octs level-by-level.
           For each oct, only the position, index, level and domain
           are needed - its position in the octree is found automatically.
           The most important is finding all the information to feed
           oct_handler.add
        """
        self.oct_handler = RAMSESOctreeContainer(self.ds.domain_dimensions/2,
                self.ds.domain_left_edge, self.ds.domain_right_edge)
        root_nodes = self.amr_header['numbl'][self.ds.min_level,:].sum()
        self.oct_handler.allocate_domains(self.total_oct_count, root_nodes)
        f = open(self.amr_fn, "rb")
        f.seek(self.amr_offset)
        mylog.debug("Reading domain AMR % 4i (%0.3e, %0.3e)",
            self.domain_id, self.total_oct_count.sum(), self.ngridbound.sum())
        def _ng(c, l):
            if c < self.amr_header['ncpu']:
                ng = self.amr_header['numbl'][l, c]
            else:
                ng = self.ngridbound[c - self.amr_header['ncpu'] +
                                self.amr_header['nboundary']*l]
            return ng
        min_level = self.ds.min_level
        # yt max level is not the same as the RAMSES one.
        # yt max level is the maximum number of additional refinement levels
        # so for a uni grid run with no refinement, it would be 0.
        # So we initially assume that.
        max_level = 0
        nx, ny, nz = (((i-1.0)/2.0) for i in self.amr_header['nx'])
        ndim = self.ds.dimensionality
        for level in range(self.amr_header['nlevelmax']):
            # Easier if do this 1-indexed
            for cpu in range(self.amr_header['nboundary'] + self.amr_header['ncpu']):
                #ng is the number of octs on this level on this domain
                ng = _ng(cpu, level)
                if ng == 0:
                    continue
                # read grid index, one integer
                ind = fpu.read_vector(f, "I").astype("int64")  # NOQA
                # skip 2 integers for next and previous index
                fpu.skip(f, 2)
                # read grid centers, ndim doubles
                pos = np.ones((ng, 3), dtype='float64') * 0.5
                pos[:, 0] = fpu.read_vector(f, "d") - nx
                if ndim > 1:
                    pos[:, 1] = fpu.read_vector(f, "d") - ny
                if ndim > 2:
                    pos[:, 2] = fpu.read_vector(f, "d") - nz
                # We want to skip the following fields:
                # father index (one integer),
                # neighbor index (2*ndim integers),
                # child index (2**ndim integers),
                # cpu map  (2**ndim integers),
                # refinement map (2**ndim integers)
                fpu.skip(f, 1 + 2*ndim + 3*2**ndim)
                # Check for duplicate grids in the octree structure
                # Note that we're adding *grids*, not individual cells.
                if level >= min_level:
                    assert(pos.shape[0] == ng)
                    n = self.oct_handler.add(cpu + 1, level - min_level, pos,
                                count_boundary = 1)
                    self._error_check(cpu, level, pos, n, ng, (nx, ny, nz))
                    if n > 0:
                        max_level = max(level - min_level, max_level)
        self.max_level = max_level
        self.oct_handler.finalize()

    def _error_check(self, cpu, level, pos, n, ng, nn):
        # NOTE: We have the second conditional here because internally, it will
        # not add any octs in that case.
        if n == ng or cpu + 1 > self.oct_handler.num_domains:
            return
        # This is where we now check for issues with creating the new octs, and
        # we attempt to determine what precisely is going wrong.
        # These are all print statements.
        print("We have detected an error with the construction of the Octree.")
        print("  The number of Octs to be added :  %s" % ng)
        print("  The number of Octs added       :  %s" % n)
        print("  Level                          :  %s" % level)
        print("  CPU Number (0-indexed)         :  %s" % cpu)
        for i, ax in enumerate('xyz'):
            print("  extent [%s]                     :  %s %s" % \
            (ax, pos[:,i].min(), pos[:,i].max()))
        print("  domain left                    :  %s" % \
            (self.ds.domain_left_edge,))
        print("  domain right                   :  %s" % \
            (self.ds.domain_right_edge,))
        print("  offset applied                 :  %s %s %s" % \
            (nn[0], nn[1], nn[2]))
        print("AMR Header:")
        for key in sorted(self.amr_header):
            print("   %-30s: %s" % (key, self.amr_header[key]))
        raise RuntimeError

    def included(self, selector):
        if getattr(selector, "domain_id", None) is not None:
            return selector.domain_id == self.domain_id
        domain_ids = self.oct_handler.domain_identify(selector)
        return self.domain_id in domain_ids

class RAMSESDomainSubset(OctreeSubset):

    _domain_offset = 1
    _block_reorder = "F"

    def fill(self, content, fields, selector, file_handler):
        # Here we get a copy of the file, which we skip through and read the
        # bits we want.
        oct_handler = self.oct_handler
        ndim = self.ds.dimensionality
        all_fields = [f for ft, f in file_handler.field_list]
        fields = [f for ft, f in fields]
        tr = {}
        cell_count = selector.count_oct_cells(self.oct_handler, self.domain_id)
        levels, cell_inds, file_inds = self.oct_handler.file_index_octs(
            selector, self.domain_id, cell_count)
        # Initializing data container
        for field in fields:
            tr[field] = np.zeros(cell_count, 'float64')

        # Loop over levels
        for level, offset in enumerate(file_handler.offset):
            if offset == -1: continue
            content.seek(offset)
            nc = file_handler.level_count[level]
            tmp = {}
            # Initalize temporary data container for io
            for field in all_fields:
                tmp[field] = np.empty((nc, 2**ndim), dtype="float64")
            for i in range(2**ndim):
                # Read the selected fields
                for field in all_fields:
                    if field not in fields:
                        fpu.skip(content)
                    else:
                        tmp[field][:,i] = fpu.read_vector(content, 'd') # i-th cell
                        import pdb; pdb.set_trace()
            import pdb; pdb.set_trace()
            oct_handler.fill_level(level, levels, cell_inds, file_inds, tr, tmp)
        return tr

class RAMSESIndex(OctreeIndex):

    def __init__(self, ds, dataset_type='ramses'):
        self.fluid_field_list = ds._fields_in_file
        self.dataset_type = dataset_type
        self.dataset = weakref.proxy(ds)
        self.index_filename = self.dataset.parameter_filename
        self.directory = os.path.dirname(self.index_filename)
        self.max_level = None

        self.float_type = np.float64
        super(RAMSESIndex, self).__init__(ds, dataset_type)

    def _initialize_oct_handler(self):
        if self.ds._bbox is not None:
            cpu_list = get_cpu_list(self.dataset, self.dataset._bbox)
        else:
            cpu_list = range(self.dataset['ncpu'])

        self.domains = [RAMSESDomainFile(self.dataset, i + 1)
                        for i in cpu_list]
        total_octs = sum(dom.local_oct_count #+ dom.ngridbound.sum()
                         for dom in self.domains)
        self.max_level = max(dom.max_level for dom in self.domains)
        self.num_grids = total_octs

    def _detect_output_fields(self):
        dsl = set([])

        # Get the detected particle fields
        for domain in self.domains:
            for ph in domain.particle_handlers:
                dsl.update(set(ph.field_offsets.keys()))

        self.particle_field_list = list(dsl)

        # Get the detected fields
        dsl = set([])
        for fh in self.domains[0].field_handlers:
            dsl.update(set(fh.field_list))
        self.fluid_field_list = list(dsl)

        self.field_list = self.particle_field_list + self.fluid_field_list

    def _identify_base_chunk(self, dobj):
        if getattr(dobj, "_chunk_info", None) is None:
            domains = [dom for dom in self.domains if
                       dom.included(dobj.selector)]
            base_region = getattr(dobj, "base_region", dobj)
            if len(domains) > 1:
                mylog.debug("Identified %s intersecting domains", len(domains))
            subsets = [RAMSESDomainSubset(base_region, domain, self.dataset)
                       for domain in domains]
            dobj._chunk_info = subsets
        dobj._current_chunk = list(self._chunk_all(dobj))[0]

    def _chunk_all(self, dobj):
        oobjs = getattr(dobj._current_chunk, "objs", dobj._chunk_info)
        yield YTDataChunk(dobj, "all", oobjs, None)

    def _chunk_spatial(self, dobj, ngz, sort = None, preload_fields = None):
        sobjs = getattr(dobj._current_chunk, "objs", dobj._chunk_info)
        for i, og in enumerate(sobjs):
            if ngz > 0:
                g = og.retrieve_ghost_zones(ngz, [], smoothed=True)
            else:
                g = og
            yield YTDataChunk(dobj, "spatial", [g], None)

    def _chunk_io(self, dobj, cache = True, local_only = False):
        oobjs = getattr(dobj._current_chunk, "objs", dobj._chunk_info)
        for subset in oobjs:
            yield YTDataChunk(dobj, "io", [subset], None, cache = cache)

    def _initialize_level_stats(self):
        levels=sum([dom.level_count for dom in self.domains])
        desc = {'names': ['numcells','level'],
                'formats':['Int64']*2}
        max_level=self.dataset.min_level+self.dataset.max_level+2
        self.level_stats = blankRecordArray(desc, max_level)
        self.level_stats['level'] = [i for i in range(max_level)]
        self.level_stats['numcells'] = [0 for i in range(max_level)]
        for level in range(self.dataset.min_level+1):
            self.level_stats[level+1]['numcells']=2**(level*self.dataset.dimensionality)
        for level in range(self.max_level+1):
            self.level_stats[level+self.dataset.min_level+1]['numcells'] = levels[level]

    def _get_particle_type_counts(self):
        npart = 0
        npart = {k: 0 for k in self.ds.particle_types
                 if k is not 'all'}
        for dom in self.domains:
            for fh in dom.particle_handlers:
                count = fh.local_particle_count
                npart[fh.ptype] += count

        return npart

    def print_stats(self):
        '''
        Prints out (stdout) relevant information about the simulation

        This function prints information based on the fluid on the grids,
        and therefore does not work for DM only runs.
        '''
        if not self.fluid_field_list:
            print("This function is not implemented for DM only runs")
            return

        self._initialize_level_stats()

        header = "%3s\t%14s\t%14s" % ("level", "# cells","# cells^3")
        print(header)
        print("%s" % (len(header.expandtabs())*"-"))
        for level in range(self.dataset.min_level+self.dataset.max_level+2):
            print("% 3i\t% 14i\t% 14i" % \
                  (level,
                   self.level_stats['numcells'][level],
                   np.ceil(self.level_stats['numcells'][level]**(1./3))))
        print("-" * 46)
        print("   \t% 14i" % (self.level_stats['numcells'].sum()))
        print("\n")

        dx = self.get_smallest_dx()
        try:
            print("z = %0.8f" % (self.dataset.current_redshift))
        except:
            pass
        print("t = %0.8e = %0.8e s = %0.8e years" % (
            self.ds.current_time.in_units("code_time"),
            self.ds.current_time.in_units("s"),
            self.ds.current_time.in_units("yr")))
        print("\nSmallest Cell:")
        for item in ("Mpc", "pc", "AU", "cm"):
            print("\tWidth: %0.3e %s" % (dx.in_units(item), item))



class RAMSESDataset(Dataset):
    _index_class = RAMSESIndex
    _field_info_class = RAMSESFieldInfo
    gamma = 1.4 # This will get replaced on hydro_fn open

    def __init__(self, filename, dataset_type='ramses',
                 fields=None, storage_filename=None,
                 units_override=None, unit_system="cgs",
                 extra_particle_fields=None, cosmological=None,
                 bbox=None):
        # Here we want to initiate a traceback, if the reader is not built.
        if isinstance(fields, string_types):
            fields = field_aliases[fields]
        '''
        fields: An array of hydro variable fields in order of position in the hydro_XXXXX.outYYYYY file
                If set to None, will try a default set of fields
        extra_particle_fields: An array of extra particle variables in order of position in the particle_XXXXX.outYYYYY file.
        cosmological: If set to None, automatically detect cosmological simulation. If a boolean, force
                      its value.
        '''
        self._fields_in_file = fields
        self._extra_particle_fields = extra_particle_fields
        self._warn_extra_fields = False
        self.force_cosmological = cosmological
        self._bbox = bbox
        Dataset.__init__(self, filename, dataset_type, units_override=units_override,
                         unit_system=unit_system)
        for FH in get_field_handlers():
            if FH.any_exist(self):
                self.fluid_types += (FH.ftype, )
        self.storage_filename = storage_filename


    def create_field_info(self, *args, **kwa):
        """Extend create_field_info to add the particles types."""
        super(RAMSESDataset, self).create_field_info(*args, **kwa)
        # Register particle filters
        if ('io', 'particle_family') in self.field_list:
            for fname, value in particle_families.items():
                def loc(val):
                    def closure(pfilter, data):
                        filter = data[(pfilter.filtered_type, "particle_family")] == val
                        return filter

                    return closure
                add_particle_filter(fname, loc(value),
                                    filtered_type='io', requires=['particle_family'])

            for k in particle_families.keys():
                mylog.info('Adding particle_type: %s' % k)
                self.add_particle_filter('%s' % k)

    def __repr__(self):
        return self.basename.rsplit(".", 1)[0]

    def _set_code_unit_attributes(self):
        """
        Generates the conversion to various physical _units based on the parameter file
        """
        # loading the units from the info file
        boxlen=self.parameters['boxlen']
        length_unit = self.parameters['unit_l']
        density_unit = self.parameters['unit_d']
        time_unit = self.parameters['unit_t']

        # calculating derived units (except velocity and temperature, done below)
        mass_unit = density_unit * length_unit**3
        magnetic_unit = np.sqrt(4*np.pi * mass_unit /
                                (time_unit**2 * length_unit))
        pressure_unit = density_unit * (length_unit / time_unit)**2

        # TODO:
        # Generalize the temperature field to account for ionization
        # For now assume an atomic ideal gas with cosmic abundances (x_H = 0.76)
        mean_molecular_weight_factor = _X**-1

        setdefaultattr(self, 'density_unit', self.quan(density_unit, 'g/cm**3'))
        setdefaultattr(self, 'magnetic_unit', self.quan(magnetic_unit, "gauss"))
        setdefaultattr(self, 'pressure_unit',
                       self.quan(pressure_unit, 'dyne/cm**2'))
        setdefaultattr(self, 'time_unit', self.quan(time_unit, "s"))
        setdefaultattr(self, 'mass_unit', self.quan(mass_unit, "g"))
        setdefaultattr(self, 'velocity_unit',
                       self.quan(length_unit, 'cm') / self.time_unit)
        temperature_unit = (
            self.velocity_unit**2*mp*mean_molecular_weight_factor/kb)
        setdefaultattr(self, 'temperature_unit', temperature_unit.in_units('K'))

        # Only the length unit get scales by a factor of boxlen
        setdefaultattr(self, 'length_unit',
                       self.quan(length_unit * boxlen, "cm"))

    def _parse_parameter_file(self):
        # hardcoded for now
        # These should be explicitly obtained from the file, but for now that
        # will wait until a reorganization of the source tree and better
        # generalization.
        self.dimensionality = 3
        self.refine_by = 2
        self.parameters["HydroMethod"] = 'ramses'
        self.parameters["Time"] = 1. # default unit is 1...

        self.unique_identifier = \
            int(os.stat(self.parameter_filename)[stat.ST_CTIME])
        # We now execute the same logic Oliver's code does
        rheader = {}
        f = open(self.parameter_filename)
        def read_rhs(cast):
            line = f.readline().replace('\n', '')
            p, v = line.split("=")
            rheader[p.strip()] = cast(v.strip())
        for i in range(6): read_rhs(int)
        f.readline()
        for i in range(11): read_rhs(float)
        f.readline()
        read_rhs(str)
        # This next line deserves some comment.  We specify a min_level that
        # corresponds to the minimum level in the RAMSES simulation.  RAMSES is
        # one-indexed, but it also does refer to the *oct* dimensions -- so
        # this means that a levelmin of 1 would have *1* oct in it.  So a
        # levelmin of 2 would have 8 octs at the root mesh level.
        self.min_level = rheader['levelmin'] - 1
        # Now we read the hilbert indices
        self.hilbert_indices = {}
        if rheader['ordering type'] == "hilbert":
            f.readline() # header
            for n in range(rheader['ncpu']):
                dom, mi, ma = f.readline().split()
                self.hilbert_indices[int(dom)] = (float(mi), float(ma))

        if rheader['ordering type'] != 'hilbert' and self._bbox:
            raise NotImplementedError(
                'The ordering %s is not compatible with the `bbox` argument.'
                % rheader['ordering type'])
        self.parameters.update(rheader)
        if 'ndim' in self.parameters:
            self.dimensionality = int(self.parameters['ndim'])
        self.domain_left_edge = np.zeros(3, dtype='float64')
        self.domain_dimensions = np.ones(3, dtype='int32') * \
                        2**(self.min_level+1)
        if self.dimensionality < 3:
            self.domain_dimensions[self.dimensionality:] = 1
        self.domain_right_edge = np.ones(3, dtype='float64')
        # This is likely not true, but it's not clear how to determine the boundary conditions
        self.periodicity = (True, True, True)

        if self.force_cosmological is not None:
            is_cosmological = self.force_cosmological
        else:
            # These conditions seem to always be true for non-cosmological datasets
            is_cosmological = not (rheader["time"] >= 0 and
                                   rheader["H0"] == 1 and
                                   rheader["aexp"] == 1)

        if not is_cosmological:
            self.cosmological_simulation = 0
            self.current_redshift = 0
            self.hubble_constant = 0
            self.omega_matter = 0
            self.omega_lambda = 0
        else:
            self.cosmological_simulation = 1
            self.current_redshift = (1.0 / rheader["aexp"]) - 1.0
            self.omega_lambda = rheader["omega_l"]
            self.omega_matter = rheader["omega_m"]
            self.hubble_constant = rheader["H0"] / 100.0 # This is H100
        self.max_level = rheader['levelmax'] - self.min_level - 1
        f.close()


        if self.cosmological_simulation == 0:
            self.current_time = self.parameters['time']
        else :
            self.tau_frw, self.t_frw, self.dtau, self.n_frw, self.time_tot = \
                friedman( self.omega_matter, self.omega_lambda, 1. - self.omega_matter - self.omega_lambda )

            age = self.parameters['time']
            iage = 1 + int(10.*age/self.dtau)
            iage = np.min([iage,self.n_frw//2 + (iage - self.n_frw//2)//10])

            self.time_simu = self.t_frw[iage  ]*(age-self.tau_frw[iage-1])/(self.tau_frw[iage]-self.tau_frw[iage-1])+ \
                             self.t_frw[iage-1]*(age-self.tau_frw[iage  ])/(self.tau_frw[iage-1]-self.tau_frw[iage])

            self.current_time = (self.time_tot + self.time_simu)/(self.hubble_constant*1e7/3.08e24)/self.parameters['unit_t']

        # Add the particle types
        ptypes = []
        for PH in get_particle_handlers():
            if PH.any_exist(self):
                ptypes.append(PH.ptype)

        ptypes = tuple(ptypes)
        self.particle_types = self.particle_types_raw = ptypes



    @classmethod
    def _is_valid(self, *args, **kwargs):
        if not os.path.basename(args[0]).startswith("info_"): return False
        fn = args[0].replace("info_", "amr_").replace(".txt", ".out00001")
        return os.path.exists(fn)
