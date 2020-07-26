import numpy as np
from mpi4py import MPI
import h5py
import sys
import pmesh.pm as pmesh # Particle mesh Routine
import time

import cProfile, pstats
from mpi4py.MPI import COMM_WORLD

pr = cProfile.Profile()
pr.enable()


# INITIALIZE MPIW=
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

# Set simulation input data
CONF = {}
exec(open(sys.argv[1]).read(), CONF)



# Set seed for all simulations
np.random.seed(0)

# Initialization of simulation variables
kb=2.479/298
if 'T0' in CONF:
    CONF['kbT0']=kb*CONF['T0']
    E_kin0 = kb*3*CONF['Np']*CONF['T0']/2.

if 'T_start' in CONF:
    CONF['kbT_start']=kb*CONF['T_start']

Ncells = (CONF['Nv']**3)
CONF['L']      = np.array(CONF['L'])
CONF['V']      = CONF['L'][0]*CONF['L'][1]*CONF['L'][2]
CONF['dV']     = CONF['V']/(CONF['Nv']**3)
CONF['n_frames'] = CONF['NSTEPS']//CONF['nprint'] + 1

if 'phi0' not in CONF:
    CONF['phi0']=CONF['Np']/CONF['V']
    

np_per_MPI=CONF['Np']//size
if rank==size-1:
    np_cum_mpi=[rank*np_per_MPI,CONF['Np']]
else:
    np_cum_mpi=[rank*np_per_MPI,(rank+1)*np_per_MPI]

p_mpi_range = range(np_cum_mpi[0],np_cum_mpi[1])

# Read input
f_input = h5py.File(sys.argv[2], 'r',driver='mpio', comm=MPI.COMM_WORLD)
r=f_input['coordinates'][-1,np_cum_mpi[0]:np_cum_mpi[1],:]
vel=f_input['velocities'][-1,np_cum_mpi[0]:np_cum_mpi[1],:]
f=np.zeros((len(r),3))
f_old=np.copy(f)
types=f_input['types'][np_cum_mpi[0]:np_cum_mpi[1]]
indicies=f_input['indicies'][np_cum_mpi[0]:np_cum_mpi[1]]
names = f_input['names'][np_cum_mpi[0]:np_cum_mpi[1]]
f_input.close() 


#Particle-Mesh initialization
pm   = pmesh.ParticleMesh((CONF['Nv'],CONF['Nv'],CONF['Nv']),BoxSize=CONF['L'], dtype='f8',comm=comm)
 

# INTILIZE PMESH ARRAYS
phi=[]
phi_t=[]
force_ds=[]
v_pot=[]

for t in range(CONF['ntypes']):
    phi.append(pm.create('real'))
    phi_t.append(pm.create('real'))
    v_pot.append(pm.create('real'))
    force_ds.append([pm.create('real') for d in range(3)])

# Output files
f_hd5 = h5py.File('sim.hdf5', 'w',driver='mpio', comm=MPI.COMM_WORLD)
dset_pos  = f_hd5.create_dataset("coordinates", (CONF['n_frames'],CONF['Np'],3), dtype="Float32")
dset_vel  = f_hd5.create_dataset("velocities", (CONF['n_frames'],CONF['Np'],3), dtype="Float32")

dset_pos.attrs['units']="nanometers"
dset_time = f_hd5.create_dataset("time", shape=(CONF['n_frames'],), dtype="Float32")

dset_time.attrs['units']="picoseconds"
dset_names=f_hd5.create_dataset("names",  (CONF['Np'],), dtype="S5")
dset_indicies=f_hd5.create_dataset("indicies",  (CONF['Np'],), dtype='i')
dset_types=f_hd5.create_dataset("types",  (CONF['Np'],), dtype='i')
dset_lengths=f_hd5.create_dataset("cell_lengths",  (1,3,3), dtype='Float32')
dset_tot_energy=f_hd5.create_dataset("tot_energy",  (CONF['n_frames'],), dtype='Float32')
dset_pot_energy=f_hd5.create_dataset("pot_energy",  (CONF['n_frames'],), dtype='Float32')
dset_kin_energy=f_hd5.create_dataset("kin_energy",  (CONF['n_frames'],), dtype='Float32')
dset_names=names
dset_types[np_cum_mpi[0]:np_cum_mpi[1]]=types
dset_indicies[np_cum_mpi[0]:np_cum_mpi[1]]=indicies
dset_lengths[0,0,0]=CONF["L"][0]
dset_lengths[0,1,1]=CONF["L"][1]
dset_lengths[0,2,2]=CONF["L"][2]

if rank==0:
    fp_E   = open('E.dat','w')

#FUNCTION DEFINITIONS

def GEN_START_VEL(vel):
    #NORMAL DISTRIBUTED PARTICLES FIRST FRAME
    std  = np.sqrt(CONF['kbT_start']/CONF['mass'])
    vel = np.random.normal(loc=0, scale=std,size=(CONF['Np'],3))
    
    vel = vel-np.mean(vel, axis=0)
    fac= np.sqrt((3*CONF['Np']*CONF['kbT_start']/2.)/(0.5*CONF['mass']*np.sum(vel**2)))
    
    return fac*vel

 
def GEN_START_UNIFORM(r):
     
    n=int(np.ceil(CONF['Np']**(1./3.)))
    l=CONF['L'][0]/n
    x=np.linspace(0.5*l,(n-0.5)*l,n)
    
    j=0
    for ix in range(n):
        for iy in range(n):
            for iz in range(n):
                if(j<CONF['Np']):
                    r[j,0]=x[ix]
                    r[j,1]=x[iy]
                    r[j,2]=x[iz]
                
                j+=1
    if(j<CONF['Np']):
        r[j:]=CONF['L']*np.random.random((CONF['Np']-j,3))
    return r


def INTEGERATE_POS(x, vel, a):
# Velocity Verlet integration I
    return x + vel*CONF['dt'] + 0.5*a*CONF['dt']**2

def INTEGRATE_VEL(vel, a, a_old):
# Velocity Verlet integration II
    return vel + 0.5*(a+a_old)*CONF['dt']

def VEL_RESCALE(vel, tau):

    #https://doi.org/10.1063/1.2408420

    # INITIAL KINETIC ENERGY
    E_kin  = comm.allreduce(0.5*CONF['mass']*np.sum(vel**2))


    #BERENDSEN LIKE TERM
    d1 = (E_kin0-E_kin)*CONF['dt']/tau

    # WIENER NOISE
    dW = np.sqrt(CONF['dt'])*np.random.normal()

    # STOCHASTIC TERM
    d2 = 2*np.sqrt(E_kin*E_kin0/(3*CONF['Np']))*dW/np.sqrt(tau)

    # TARGET KINETIC ENERGY
    E_kin_target = E_kin + d1 + d2

    #VELOCITY SCALING
    alpha = np.sqrt(E_kin_target/E_kin)

    vel=vel*alpha
    
    return vel

def STORE_DATA(step,frame):


    # dset_pos[frame,np_cum_mpi[0]:np_cum_mpi[1]]=r
    # dset_vel[frame,np_cum_mpi[0]:np_cum_mpi[1]]=vel
    dset_pos[frame,indicies] = r
    dset_vel[frame,indicies] = vel
    
    
    if rank==0:
        dset_tot_energy[frame]=W+E_kin
        dset_pot_energy[frame]=W
        dset_kin_energy[frame]=E_kin
        dset_time[frame]=CONF['dt']*step

        fp_E.write("%f\t%f\t%f\t%f\t%f\t%f\t%f\t%f\n"%(step*CONF['dt'],W+E_kin,W,E_kin,T,mom[0],mom[1],mom[2]))
        fp_E.flush()



def COMP_FORCE(f, r, force_ds):
    for t in range(CONF['ntypes']):
        for d in range(3):
            f[types==t, d] = force_ds[t][d].readout(r[types==t], layout=layouts[t])

def COMP_PRESSURE():
    # COMPUTES HPF PRESSURE FOR EACH MPI TASK
    P=[]
    p_val=[]
    for d in range(3):
        
        p = CONF['w'](phi_t)
        for t in range(CONF['ntypes']):
            p += -v_pot[t]*phi_t[t] + (v_pot[t].r2c(out=Ellipsis).apply(CONF['kdHdk'])[d]).c2r(out=Ellipsis)


        P.append(p*CONF['dV']/CONF['V'])
        p_val.append(p.csum())
    return np.array(p_val)
      
def UPDATE_FIELD(layouts,comp_v_pot=False):

    # Filtered density
    for t in range(CONF['ntypes']):
        p = pm.paint(r[types==t], layout=layouts[t])
        p = p/CONF['dV']
        phi_t[t] = p.r2c(out=Ellipsis).apply(CONF['H'], out=Ellipsis).c2r(out=Ellipsis)

    # External potential
    for t in range(CONF['ntypes']):
        v_p_fft=CONF['V_EXT'][t](phi_t).r2c(out=Ellipsis).apply(CONF['H'], out=Ellipsis)
   
        # Derivative of external potential
        for d in range(3):    
            def force_transfer_function(k, v, d=d):
                return -k[d] * 1j * v 
            force_ds[t][d]=(v_p_fft.copy().apply(force_transfer_function).c2r(out=Ellipsis))
 
        if(comp_v_pot):
            v_pot[t]=v_p_fft.c2r(out=Ellipsis)
 
    
def DOMAIN_DECOMPOSITION(layouts):
    # Does not work
    # Problems with redefinition of array sizes
    types_cp=types.copy()
    for t in range(CONF['ntypes']):
        r[types==t]         = layouts[t].exchange(r[types==t])
        mass[types==t]      = layouts[t].exchange(mass[types==t])
        vel[types==t]       = layouts[t].exchange(vel[types==t])
        indicies[types==t]  = layouts[t].exchange(indicies[types==t])
        names[types==t]     = layouts[t].exchange(names[types==t])
        f[types==t]         = layouts[t].exchange(f[types==t])
        f_old[types==t]     = layouts[t].exchange(f_old[types==t])
        types[types_cp==t]  = layouts[t].exchange(types_cp[types==t])


def COMPUTE_ENERGY():
    E_hpf = 0    
    E_kin = pm.comm.allreduce(0.5*CONF['mass']*np.sum(vel**2))
    W = CONF['w'](phi_t)*CONF['dV']

    W = W.csum()

    return E_hpf,E_kin,W
    

def PRESET_VEL(vel,T):
    std  = np.sqrt(CONF['kbT0']/CONF['mass'])
    vel[:,0]=np.random.normal(loc=0, scale=std, size=CONF['Np'])
    vel[change,1]=np.random.normal(loc=0, scale=std, size=CONF['Np'])
    vel[change,2]=np.random.normal(loc=0, scale=std, size=CONF['Np'])
    return vel

def REMOVE_CM_VEL(vel,T=None):
    if T==None:
        E_KIN_1 = 0.5*CONF['mass']*np.sum(vel**2)
    else:
        E_KIN_1 = kb*3*CONF['Np']*T/2.
 
    vcm     = -np.mean(vel,axis=0)
    E_KIN_2 = 0.5*CONF['mass']*np.sum((vel-vcm)**2)

    return (vel-vcm)*np.sqrt(E_KIN_1/E_KIN_2)



if rank==0:
    start_t = time.time()
  

# First step

layouts  = [pm.decompose(r[types==t]) for t in range(CONF['ntypes'])]
UPDATE_FIELD(layouts,True)
COMP_FORCE(f, r, force_ds)

for step in range(CONF['NSTEPS']):
    
    frame=step//CONF['nprint']
    if(np.mod(step,CONF['nprint'])==0):      
        E_hpf, E_kin,W = COMPUTE_ENERGY()        
        T     =   2*E_kin/(kb*3*CONF['Np'])
        mom=pm.comm.allreduce(np.sum(vel,axis=0))

    f_old = np.copy(f)

    #Integrate positions
    r     = INTEGERATE_POS(r, vel, f/CONF['mass'])

    #PERIODIC BC
    r     = np.mod(r, CONF['L'][None,:])
    
    layouts  = [pm.decompose(r[types==t]) for t in range(CONF['ntypes'])]


#    DOMAIN_DECOMPOSITION(layouts)
    if(np.mod(step+1,CONF['quasi'])==0):
        UPDATE_FIELD(layouts, np.mod(step+1,CONF['quasi'])==0)
         

    COMP_FORCE(f,r,force_ds)

    
    # Integrate velocity
    vel = INTEGRATE_VEL(vel, f/CONF['mass'], f_old/CONF['mass'])

    # Thermostat
    if('T0' in CONF):
        vel = VEL_RESCALE(vel,tau)

    # Print trajectory
    if(np.mod(step,CONF['nprint'])==0):
        STORE_DATA(step, frame)

# End simulation
if rank==0:
    print('Simulation time elapsed:', time.time()-start_t)

        
UPDATE_FIELD(layouts,True)

frame=(step+1)//CONF['nprint']

E_hpf, E_kin,W = COMPUTE_ENERGY()        
T     =   2*E_kin/(kb*3*CONF['Np'])

STORE_DATA(step,frame)
 
pr.disable()

# Dump results:
# - for binary dump
#pr.dump_stats('cpu_%d.pstat' %comm.rank)

# stats = pstats.Stats(pr,'profile_stats_%d.pstat'%comm.rank).sort_stats('tottime')

#ps = pstats.Stats(pr, stream=s).sort_stats('tottime')
#ps.print_stats()

# - for text dump
f_hd5.close()
with open( 'cpu_%d.txt' %comm.rank, 'w') as output_file:
    sys.stdout = output_file
    pr.print_stats( sort='time' )
    sys.stdout = sys.__stdout__
