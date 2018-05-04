#!/usr/bin/env python
import numpy as np
from mpi4py import MPI
from time import time
from behalf import initialConditions
from behalf import integrator
from behalf import utils
import sys
import argparse
import os


comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()


if __name__ == '__main__':
    # Our unit system:
    # Length: kpc
    # Time: Myr
    # Mass: 10^9 M_sun

    GRAV_CONST = 4.483e-3  # Newton's Constant, in kpc^3 GM_sun^-1 Myr^-2
    THETA = 0.5

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--run-name', help='Name of the run',
                        type=str, required=True)
    parser.add_argument('--N-parts', help='Number of particles', type=int,
                        required=True)
    parser.add_argument('--total-mass', help='Total mass of the system (in GMsun)',
                        type=float, default=1e5)
    parser.add_argument('--radius', help='Scale Radius (in kpc)',
                        type=float, default=10.)
    parser.add_argument('--N-steps', help='Number of time steps',
                        type=int, default=1000)
    parser.add_argument('--dt', help='Size of time step (in Myr)',
                        type=float, default=0.01)
    parser.add_argument('--softening', help='Softening length (in kpc)',
                        type=float, default=0.01)
    parser.add_argument('--save_every', help='How often to save output results',
                        type=int, default=10)
    parser.add_argument('--rand-seed', help='Random seed to initialize',
                        type=int, default=1234)
    parser.add_argument('--clobber', help='Should previous results be overwritten?',
                        action='store_true')
    parser.add_argument('--verbose', help='Should diagnostics be printed?',
                        action='store_true')
    args = parser.parse_args()

    run_name = args.run_name
    M_total = args.total_mass  # total mass of system (in 10^9 M_sun)
    N_parts = args.N_parts  # how many particles?
    M_part = M_total / N_parts  # mass of each particle (in 10^9 M_sun)
    a = args.radius  # scale radius (in kpc)
    N_steps = args.N_steps  # how many time steps?
    dt = args.dt  # size of time step (in Myr)
    softening = args.softening  # softening length (in kpc)
    save_every = args.save_every  # how often to save output results
    seed = args.rand_seed  # Initialize state identically every time
    clobber = args.clobber
    verbose = args.verbose
    
    # If we split "N_parts" particles into "size" chunks,
    # which particles does each process get?
    part_ids_per_process = np.array_split(np.arange(N_parts), size)
    # How many particles are on each processor?
    N_per_process = np.array([len(part_ids_per_process[p])
                              for p in range(size)])
    # data-displacements, for transferring data to-from processor
    displacements = np.insert(np.cumsum(N_per_process * 3), 0, 0)[0:-1]
    # How many particles on this process?
    N_this = N_per_process[rank]

    if rank == 0:
        results_dir = run_name + '/'
        if os.path.exists(results_dir) and not clobber:
            assert False, 'Directory already exists, and clobber not set'
        else:
            os.makedirs(results_dir)
        
        # Set Plummer Sphere (or other) initial conditions
        pos_init, vel_init = initialConditions.plummer(N_parts, a, m=M_part,
                                                       G=GRAV_CONST, seed=seed)
        # Set center-of-mass and mean velocity to zero
        pos_init -= np.mean(pos_init, axis=0)
        vel_init -= np.mean(vel_init, axis=0)
        masses = np.ones(N_parts) * M_part
        
        # Self-start the Leap-Frog algorithm, all on the main node
        # Construct the tree and compute forces
        tree = utils.construct_tree(pos_init, masses)
        accels = utils.compute_accel(tree, np.arange(N_parts),
                                     THETA, GRAV_CONST, eps=softening)
        # Half-kick
        _, vel_full = integrator.cuda_timestep(pos_init, vel_init, accels,
                                               dt/2.)
        # Full-drift
        pos_full, _ = integrator.cuda_timestep(pos_init, vel_full, accels, dt)
        # From now on, the Leapfrog algorithm can do Full-Kick + Full-Drift
    else:
        pos_full, vel_full = None, None
    
    # The main integration loop
    if rank == 0:
        if verbose:
            print('Starting Integration Loop')
            sys.stdout.flush()
        t_start = time()
    for i in range(N_steps):
        # Construct the tree and compute forces
        if rank == 0:
            tree = utils.construct_tree(pos_full, masses)
        else:
            tree = None
        # broadcast the tree
        tree = comm.bcast(tree, root=0)
        # scatter the positions and velocities
        pos = np.empty((N_this, 3))
        vel = np.empty((N_this, 3))
        comm.Scatterv([pos_full, N_per_process*3, displacements, MPI.DOUBLE],
                      pos, root=0)
        comm.Scatterv([vel_full, N_per_process*3, displacements, MPI.DOUBLE],
                      vel, root=0)
        # compute forces
        accels = utils.compute_accel(tree, part_ids_per_process[rank],
                                     THETA, GRAV_CONST, eps=softening)
        # forward one time step
        pos, vel = integrator.cuda_timestep(pos, vel, accels, dt)
        # gather the positions and velocities
        pos_full = None
        vel_full = None
        if rank == 0:
            pos_full = np.empty((N_parts, 3))
            vel_full = np.empty((N_parts, 3))
        comm.Gatherv(pos, [pos_full, N_per_process*3, displacements, MPI.DOUBLE],
                     root=0)
        comm.Gatherv(vel, [vel_full, N_per_process*3, displacements, MPI.DOUBLE],
                     root=0)

        if rank == 0:
            # Print status
            if verbose:
                print('Iteration {:d} complete. {:.1f} seconds elapsed.'.format(i, time() - t_start))
                sys.stdout.flush()
            # Save the results to output file
            if ((i % save_every) == 0) or (i == N_steps - 1):
                utils.save_results(results_dir + 'step_{:d}.dat'.format(i), pos_full, vel_full, t_start, i, N_steps,
                                   size)