# linear interploation for triangles in both space and time
#todo  are BC cords as np.float32, faster as lower memory transfer demand and good enough?
import numpy as np
from scipy.spatial import cKDTree

from oceantracker.interpolator._base_interp import _BaseInterp
from oceantracker.util import basic_util
from oceantracker.util.profiling_util import function_profiler
from time import perf_counter
from oceantracker.util import numpy_util

from oceantracker.interpolator.util import triangle_interpolator_util as tri_interp_util ,  triangle_eval_interp

from oceantracker.util.parameter_checking import  ParamValueChecker as PVC


class  InterpTriangularNativeGrid_Slayer_and_LSCgrid(_BaseInterp):

    # uses tweaked sci py which allows using start triangle location

    def __init__(self):
        # set up info/attributes
        super().__init__()
        self.add_default_params({'bc_walk_tol': PVC(1.0e-5, float,min = 0.),
                                 'max_search_steps': PVC(500,int, min =1)})
        self.info['current_buffer_index'] = np.zeros((2,), dtype=np.int32)

    #@function_profiler(__name__)
    def initial_setup(self):
        super().initial_setup()  # children must call this parent class to default shared_params etc
        params = self.params
        si= self.shared_info
        grid = si.classes['reader'].grid

        # make barcentric transform matrix for the grid,  typee to match numba signature
        t0 = perf_counter()
        grid['bc_transform'] = tri_interp_util.get_BC_transform_matrix(grid['x'].data, grid['triangles'].data).astype(np.float64)
        si.msg_logger.progress_marker('built barycentric-transform matrix', start_time=t0)
        # build kd tree for initial triangle find node nearest node locations
        #todo delete xy_centriod = np.mean(grid['x'][grid['triangles']], axis=1)
        #todo deleteself.KDtree = cKDTree(xy_centriod)
        self.KDtree = cKDTree(grid['x'])

        # create particle properties to  store history of current triangle  for reuse

        p = si.classes['particle_group_manager']
        p.create_particle_property('n_cell', 'manual_update',dict(write=False, dtype=np.int32, initial_value=0))  # start with cell number guess of zero
        p.create_particle_property('n_cell_last_good', 'manual_update', dict(write=False, dtype=np.int32, initial_value=0))  # start with cell number guess of zero
        p.create_particle_property('bc_cords','manual_update',dict(  write=False, initial_value=0., vector_dim=3,dtype=np.float64))

        # BC walk info
        if si.hydro_model_is3D:
            # space to record vertical cell for each particles' triangle at two timer steps  for each node in cell containing particle
            # used to do 3D time dependent interpolation
            p.create_particle_property('nz_cell', 'manual_update',dict( write=False, dtype=np.int32, initial_value=grid['zlevel'].shape[2]-2)) # todo  create  initial serach for vertical cell
            p.create_particle_property('z_fraction','manual_update',dict(   write=False, dtype=np.float32, initial_value=0.))
            p.create_particle_property('z_fraction_bottom_layer','manual_update', dict( write=False, dtype=np.float32, initial_value=0., description=' thickness of bottom layer in metres, used for log layer velocity interp in bottom layer'))


        # attach a reader to this interpolator
        self.reader = si.classes['reader']

    def final_setup(self):

        # set up a grid class,part_prop and vertical cell find functions to minimise numba function arguments
        si = self.shared_info
        reader = self.reader
        self.grid = reader.grid
        grid = self.grid
        fi = reader.info['file_info']
        bi = reader.info['buffer_info']
        info = self.info
        info['current_hydro_model_step']= 0
        info['current_fractional_time_steps']= np.zeros((2,), dtype=np.float64)
        info['current_buffer_steps'] = np.zeros((2,), dtype=np.int32)

        # set up place for walk info failures
        info['tri_walk_full_failures'] = []

        # set up walk count vector and map to info
        self.walk_counts=np.zeros((8,),dtype=np.int64)
        wc = self.walk_counts
        info.update(dict(   particles_located_by_walking = wc[0:1],
                            number_of_triangles_walked = wc[1:2],
                            longest_triangle_walk = wc[2:3],
                            nans_encountered_triangle_walk = wc[3:4],
                            triangle_walks_retried = wc[4:5],
                            particles_killed_after_triangle_walk_retry_failed = wc[5:6],
                            total_vertical_steps_walked = wc[6:7],
                            longest_vertical_walk = wc[7:8])
                            )

        # location of each vertex
        grid['x_vertex'] = np.stack((grid['x'][grid['triangles'], 0], grid['x'][grid['triangles'], 1]), axis=2)

        # build triangle walk array of structures
        grid['tri_walk_AOS'] =numpy_util.numpy_array_of_structures_from_dict(
                                     dict(bc_transform= grid['bc_transform'],
                                        adjacency=  grid['adjacency']),
                                        )



    def setup_interp_time_step(self, time_sec, xq, active):
        # set up stuff needed by all fields before any 2D interpolation
        # eg query point and nt the current global time step, from which we are making nt+1
        si = self.shared_info
        info = self.info
        reader = self.reader
        fi = reader.info['file_info']
        bi = reader.info['buffer_info']
        grid = reader.grid


        # set buffer index from this time and next inside stepinfo
        # get next two buffer time steps around the given time in reader ring buffer
        # plus global time step locations and time ftactions od timre step
        # put results in interpolators step info numpy structure
        hindcast_fraction = (time_sec - fi['first_time']) / (fi['last_time']- fi['first_time'])
        info['current_hydro_model_step'] = int((fi['n_time_steps_in_hindcast'] - 1) * hindcast_fraction)  # global hindcast time step

        # ring buffer locations of surounding steps
        info['current_buffer_steps'][0] = info['current_hydro_model_step'] %  bi['buffer_size']
        info['current_buffer_steps'][1] = (info['current_hydro_model_step'] + int(si.model_direction)) %  bi['buffer_size']

        time_hindcast = grid['time'][  info['current_buffer_steps'][0]]

        # sets the fraction of time step that current time is between
        # surrounding hindcast time steps
        # abs makes it work when backtracking
        s = abs(time_sec - time_hindcast) / fi['hydro_model_time_step']
        info['current_fractional_time_steps'][0] = s
        info['current_fractional_time_steps'][1] = 1.0 - s

        # find cell for xq, node list and weight for interp at calls
        self.find_cell(xq, active)

    def check_steps_in_reader_buffer(self,time_sec):
        if not self.reader.are_time_steps_in_buffer(time_sec):
            self.reader.fill_time_buffer(time_sec)  # get next steps into buffer if not in buffer

    # @function_profiler(__name__)
    def eval_water_velocity_at_particle_locations(self,output, active ):
        # interp reader fieldName inplace to particle locations to same time and memory
        # output can optionally be redirected to another particle property name different from  reader's fieldName
        # particle_prop_name
        # in place evaluation of field interpolation
        si = self.shared_info
        grid = self.grid
        field_instance = si.classes['fields']['water_velocity']
        part_prop = si.classes['particle_properties']
        n_cell = part_prop['n_cell'].data
        bc_cords = part_prop['bc_cords'].data
        info = self.info
        nb = info['current_buffer_steps']
        fractional_time_steps = info['current_fractional_time_steps']

        if field_instance.is3D():
            nz_cell = part_prop['nz_cell'].data
            triangles = grid['triangles']
            bottom_cell_index = grid['bottom_cell_index']
            z_fraction = part_prop['z_fraction'].data
            z_fraction_bottom_layer = part_prop['z_fraction_bottom_layer'].data

            triangle_eval_interp.eval_water_velocity_3D(nb, fractional_time_steps, basic_util.atLeast_Nby1(output),
                                               field_instance.data,
                                               triangles, bottom_cell_index,
                                               n_cell, bc_cords, nz_cell, z_fraction, z_fraction_bottom_layer,
                                               active)
        else:
            self._interp_field2D(field_instance, n_cell, bc_cords, output, active)

    #@function_profiler(__name__)
    def interp_field_at_current_particle_locations(self, field_instance, active, output):
        # interp reader fieldName inplace to particle locations to same time and memory
        # output can optionally be redirected to another particle property name different from  reader's fieldName
        # particle_prop_name
       # in place evaluation of field interpolation
        si = self.shared_info
        grid = self.grid
        part_prop = si.classes['particle_properties']
        n_cell = part_prop['n_cell'].data
        bc_cords = part_prop['bc_cords'].data

        if field_instance.is3D():
            nz_cell = part_prop['nz_cell'].data
            z_fraction = part_prop['z_fraction'].data
            bottom_cell_index = grid['bottom_cell_index']

            self._interp_field3D(field_instance, n_cell, bc_cords, nz_cell,
                                 z_fraction,bottom_cell_index, active, output)
        else:

          self._interp_field2D(field_instance, n_cell, bc_cords,output, active)

    # @function_profiler(__name__)
    def _interp_field2D(self, field_instance, n_cell, bc_cords,output, active):
        # interp reader fieldName inplace to particle locations to same time and memory
        # output can optionally be redirected to another particle property name different from  reader's fieldName
        # particle_prop_name
        # in place evaluation of field interpolation
        si = self.shared_info
        part_prop = si.classes['particle_properties']
        grid = self.grid
        triangles = grid['triangles']
        info = self.info
        nb = info['current_buffer_steps']
        fractional_time_steps = info['current_fractional_time_steps']

        if field_instance.is_time_varying():
            triangle_eval_interp.time_dependent_2Dfield(nb, fractional_time_steps,basic_util.atLeast_Nby1(output),
                                                   field_instance.data, triangles,
                                                   n_cell, bc_cords,
                                                   active)
        else:
            triangle_eval_interp.time_independent_2Dfield(basic_util.atLeast_Nby1(output),
                                                 field_instance.data, triangles,
                                                 n_cell, bc_cords,
                                                 active)

    # @function_profiler(__name__)
    def _interp_field3D(self, field_instance, n_cell, bc_cords,
                        nz_cell, z_fraction, bottom_cell_index,active, output):
        # interp reader fieldName inplace to particle locations to same time and memory
        # output can optionally be redirected to another particle property name different from  reader's fieldName
        # particle_prop_name
        # in place evaluation of field interpolation
        si = self.shared_info
        grid = self.grid
        triangles = grid['triangles']
        info = self.info

        if field_instance.is_time_varying():
            triangle_eval_interp.time_dependent_3Dfield(info['current_buffer_steps'], info['current_fractional_time_steps'],
                                                    field_instance.data,
                           triangles, bottom_cell_index,
                           n_cell, bc_cords, nz_cell, z_fraction,
                           basic_util.atLeast_Nby1(output), active)
        else:
            # 3D non-time varying
            # todo eval_interp3D_timeIndependent not implented for 3D non-time varying fields
            raise Exception('eval_field_interpolation_at_particle_locations : spatial interp using eval_interp3D_timeIndependent not implemented yet ')

    # @function_profiler(__name__)
    def eval_field_interpolation_at_given_locations(self, field_instance, x, time=None, output=None, n_cell=None):
        # in  evaluation of field interpolation at specific locations, ie not particle locations
        # todo only time_dependent_2Dfield  working - eval_field_interpolation_at_given_locations
        # todo add time dependence/ time fractions
        # does this over write paricle props??
        si = self.shared_info
        reader = si.classes['reader']

        part_prop = si.classes['particle_properties']
        grid = self.grid

        # is no output name give particle property for output is same as hindcast fieldName
        if output is None:
            if field_instance.data.shape[3] > 1:
                output = np.full((x.shape[0], field_instance.data.shape[3]), np.nan)
            else:
                output = np.full((x.shape[0],), np.nan)

        if n_cell is None:
            n_cell = self.initial_cell_guess(x)

        # get bc cords for the cells
        bc_cords = self.get_bc_cords(x, n_cell)
        active = np.arange(x.shape[0])
        if time is  None:
            # not time varing
            nb = None
            nt = None
        else:
            nt = reader.time_to_hydro_model_index(time)
            nb = reader.buffer_index_to_buffer_offset(nt)

        if field_instance.is3D():
            nz_cell = part_prop['nz_cell'].data
            z_fraction = part_prop['z_fraction'].data
            bottom_cell_index = grid['bottom_cell_index']

            self._interp_field3D(field_instance, n_cell, bc_cords, nz_cell,
                                 z_fraction,bottom_cell_index, active, output)
        else:

          self._interp_field2D( field_instance, n_cell, bc_cords,output, active)


        return output

    def find_cell(self, xq, active):
        # locate cell in place
        # nt give but not needed in 2D
        si= self.shared_info
        info = self.info

        # used 2D or 3D walk chosen above
        self._do_walk(xq, active)

        #retry any too long wallks
        part_prop = si.classes['particle_properties']

        sel = part_prop['status'].find_subset_where(active, 'eq', si.particle_status_flags['cell_search_failed'], out =self.get_partID_subset_buffer('B1'))
        if sel.size > 0:
            info['triangle_walks_retried'] += sel.size
            new_cell  = self.initial_cell_guess(xq[sel,:])
            part_prop['n_cell'].set_values(new_cell, sel)

            self._do_walk(xq, sel)

            # recheck for additional failures
            sel = part_prop['status'].find_subset_where(sel, 'eq', si.particle_status_flags['cell_search_failed'], out=self.get_partID_subset_buffer('B1'))
            if sel.size > 0:
                wf = {'x0': part_prop['x_last_good'].get_values(sel),
                      'xq': part_prop['x'].get_values(sel)}

                info['tri_walk_full_failures'].append(wf)
                info['particles_killed_after_triangle_walk_retry_failed'] += sel.size # total failed walks
                si.msg_logger.msg('walks too long after kd retry- killed ' + str(sel.shape[0]) + ' particles',warning=True,tabs=0,
                                  hint='Try decreasing time step or increasing interpolator parameter "max_search_steps", current value =' + str(self.params['max_search_steps']))
                # make notes for log file enabling follow up
                si.msg_logger.msg('particle locations of failed walks, first 3 or less ', warning=True,tabs=2)
                si.msg_logger.msg(' location xq =' + str(xq[sel[:3], :].tolist()), warning=True,tabs=2)
                si.msg_logger.msg(' x_old =' + str(part_prop['x_last_good'].data[sel[:3], :].tolist()),warning=True,tabs=2)
                # kill particles
                part_prop['status'].set_values(si.particle_status_flags['dead'], sel)

    def _do_walk(self, xq, active):
        si= self.shared_info
        t0= perf_counter()
        info = self.info
        part_prop = si.classes['particle_properties']
        x_last_good = part_prop['x_last_good'].data
        n_cell = part_prop['n_cell'].data
        n_cell_last_good = part_prop['n_cell_last_good'].data
        status = part_prop['status'].data
        bc_cords = part_prop['bc_cords'].data
        grid = self.grid
        params = self.params


        # used 2D or 3D walk chosen above
        tri_interp_util.BCwalk_with_move_backs(
                           xq,
                           grid['tri_walk_AOS'], grid['dry_cell_index'],
                           x_last_good, n_cell,n_cell_last_good, status, bc_cords,
                           self.walk_counts,
                           params['max_search_steps'], params['bc_walk_tol'], si.settings['open_boundary_type'],si.settings['block_dry_cells'],
                           active)
        si.block_timer('Find cell, horizontal walk', t0)

        if False:
            # def check_cells_correct(bc_transform,x,n_cell,active, tol=1e-2)
            from oceantracker.util import  debug_util
            sel = debug_util.check_walk_step(si.classes['reader'].grid, part_prop, active)
            if sel.size >0:
                debug_util.plot_walk_step(xq,si.classes['reader'].grid, part_prop, sel)

            debug_util.plot_walk_step(xq, si.classes['reader'].grid, part_prop)
            pass

        if si.is_3D_run:
            t0 = perf_counter()
            nz_cell = part_prop['nz_cell'].data
            z_fraction = part_prop['z_fraction'].data
            z_fraction_bottom_layer = part_prop['z_fraction_bottom_layer'].data
            tri_interp_util.get_depth_cell_time_varying_Slayer_or_LSCgrid(xq,
                                            #grid['triangles'],grid['zlevel'],grid['bottom_cell_index'],
                                            grid['triangles'], grid['zlevel_vertex'], grid['bottom_cell_index'],
                                            si.z0,
                                            n_cell, status, bc_cords,nz_cell,z_fraction,z_fraction_bottom_layer,
                                            info['current_buffer_steps'],info['current_fractional_time_steps'],
                                            self.walk_counts,
                                            active)
            si.block_timer('Find cell, vertical walk', t0)

    #@function_profiler(__name__)
    def initial_cell_guess(self, xq):
        # find nearest cell
        si=self.shared_info
        t0 = perf_counter()
        grid = si.classes['reader'].grid

         # find nearest node
        dist, nodes = self.KDtree.query(xq[:, :2])
        nodes = nodes.astype(np.int32)  # KD tree gives int64,need for compatibility of types

        # look in triangles attached to each node for tri containing the point
        n_cell= tri_interp_util.check_if_point_inside_triangle_connected_to_node(xq, nodes,
                                     grid['node_to_tri_map'], grid['tri_per_node'],  grid['bc_transform'], self.params['bc_walk_tol'])
        # if x is nan dist is infinite
        n_cell[~np.isfinite(dist)] = -1
        si.block_timer('Initial cell guess', t0)
        return n_cell

    def are_points_inside_domain(self, xq):
        n_cell  = self.initial_cell_guess(xq)
        bc = self.get_bc_cords(xq,n_cell)
        is_inside=  np.all(np.logical_and(bc >= -self.params['bc_walk_tol'], bc  <= 1.+self.params['bc_walk_tol']),axis=1)
        return is_inside, n_cell, bc  # is inside if  magnitude of all BC < 1



    def get_bc_cords(self,x,n_cells):
        # get BC cords for given x's
        si= self.shared_info
        grid = si.classes['reader'].grid
        bc_cords = np.full((x.shape[0],3), 0.)
        tri_interp_util.get_BC_cords_numba(x, n_cells, grid['bc_transform'], bc_cords)
        return bc_cords

    def update_dry_cells(self):
        # update 0-255 dry cell index
        grid= self.grid
        info= self.info
        triangle_eval_interp.update_dry_cell_index(grid['is_dry_cell'], grid['dry_cell_index'],
                                                   info['current_buffer_steps'], info['current_fractional_time_steps'])
    def close(self):
        si=self.shared_info
        info = self.info
        # transfer walk / step info from to class attributes to write in case_info file

        # convert mapped array counts to int
        for key in info.keys():
            if isinstance(info[key], np.ndarray) and info[key].size==1:
                info[key] = int(info[key])

        info['average_number_of_triangles_walked']= info['number_of_triangles_walked']/max(1,info['particles_located_by_walking'])
        if si.is_3D_run:
            info['average_vertical_walk_steps'] = info['total_vertical_steps_walked'] / max(1, info['particles_located_by_walking'])

        f = f" Triangle walk summary: Of  {info['particles_located_by_walking']:6,d} particles located "
        f += f" {info['triangle_walks_retried']:1d}, walks were too long and were retried, "
        f += f" of these  {info['particles_killed_after_triangle_walk_retry_failed']:1d} failed after retrying and were discarded"
        si.msg_logger.progress_marker(f)

        if info['triangle_walks_retried'] > 100:
            si.msg_logger.msg(f" Of {info['particles_located_by_walking']:3d} particles located {info['triangle_walks_retried']:3d}, were to long and restated from initial guess",
                              crumbs='Interpolator calls, InterpTriangularNativeGrid_Slayer_and_LSCgrid',
                              hint= f"This number of retries can be reduced by decreasing the time step.")

        if info['particles_killed_after_triangle_walk_retry_failed'] > 0:
            si.msg_logger.msg(f" Of {info['particles_located_by_walking']:3d} particles located {info['particles_killed_after_triangle_walk_retry_failed']:3d}, failed to find cell",
                              crumbs='Interpolator calls, InterpTriangularNativeGrid_Slayer_and_LSCgrid',
                              hint= f"Try increasing interpolator parameter 'max_search_steps', current value ={self.params['max_search_steps']:3d}")



