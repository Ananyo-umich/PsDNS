"""Spectral bases for solvers

Psuedo-spectral data has two representations, one in physical space,
and one in spectral space, forming a Fourier transform pair.  For MPI
parallel computation, this data must be distributed across ranks.
PsDNS provides simple support for Fourier data using three classes.
The :class:`SpectralGrid` class contains all the information about the
physical space grid, the spectral wavenumber grid, and the domain
decomposition.  :class:`PhysicalArray` and :class:`SpectralArray` are
MPI distributed array classes that are grid aware, and provide methods
to Fourier transform between them.

The PDEs implemented in PsDNS make use of scalar, vector, and rank-2
tensor field data.  To do this, the array classes allow for arbitrary
rank tensor fields.  The dimensionality of a data array for a rank-n
tensor is n+3: the first n dimensions corresponding to the tensor
indicies (rank-0 for scalars, rank-1 for vectors) and the last three
for the spatial or wavenumber coordinate.

For parallel runs, the mesh is decomposed across multiple MPI ranks.
Currently, only a slab decomposition is supported.  The physical space
mesh is decomposed along the first axis, and the spectral mesh along
the second axis.  Grid information about the entire grid will be
referred to as *global*, whereas information about a specific rank is
*local*.
"""
from functools import cached_property
import warnings

import numpy

from mpi4py import MPI


class SpectralGrid(object):
    """Grid information for spectral arrays

    The :class:`SpectralGrid` class holds a description of data arrays
    for psuedo-spectral data in physical and in spectral space,
    including how the data is decomposed across multiple MPI ranks.
    Normally, the code creates a single instance of this class which
    is shared among all the data arrays.
    """
    def __init__(
            self, sdims, pdims=None, box_size=2*numpy.pi,
            aliasing_strategy='truncate',
            comm=MPI.COMM_WORLD
            ):
        r"""Return a new :class:`SpectralGrid` object.

        A :class:`SpectralGrid` has dimensions in both physical space,
        and in spectral space.  These can be different, to allow for
        anti-aliasing by filtering.  The global spectral dimensions of
        the grid are given by *sdims*.  This can be a 3-tuple or a
        scalar if all the dimensions are equal in size.  If the
        physical dimensions are different, they are specified by
        *pdims*.  The size of the computational box is fixed at
        :math:`2 \pi`.

        Note that the shape of the physical mesh is *pdims*.  However,
        since the spectral representation is Hermitian symmetric, only
        half the modes are retained.  So the shape of the wavenumber
        grid is::

            [ sdims[0], sdims[1], sdims[2]//2+1 ]

        Also, keep in mind that the actually fast Fourier transforms are
        performed on a padded grid of shape *pdims*.  FFT routines
        typically perform better when the length is a power of small
        primes, that consideration should be applied to the choice of
        *pdims*, *sdims* can be arbitrary as far as FFT performance.

        The default MPI communicator for the grid is ``COMM_WORLD``,
        but this can be changed by providing a communitcator to the
        *comm* argument.

        For a more detailed discussion of anti-aliasing, including
        recommended sizes for *pdims* and choices for the
        *aliasing_stragegy*, see :ref:`Keeping It Real`.  As a good
        rule of thumb, for anti-aliasing, *sdims* should be odd, and
        *pdims* should be 3/2 *sdims* (rounded up).
        """
        self._aliasing_strategy = aliasing_strategy
        #: The MPI communicator to use for array operations
        self.comm = comm
        #: The global size of the mesh in spectral space
        self.sdims = numpy.broadcast_to(numpy.atleast_1d(sdims), (3,))
        #: The global size of the mesh in physical space
        self.pdims = numpy.broadcast_to(numpy.atleast_1d(pdims), (3,)) \
            if pdims else self.sdims
        box_size = numpy.broadcast_to(numpy.atleast_1d(box_size), (3,))
        if self.sdims[0] > self.pdims[0] and self.sdims[0] % 2 == 0:
            warnings.warn(
                "Truncating to an even number of modes in x: "
                "see the manual for why you don't want to do this"
                )
        if self.sdims[1] > self.pdims[1] and self.sdims[1] % 2 == 0:
            warnings.warn(
                "Truncating to an even number of modes in y: "
                "see the manual for why you don't want to do this"
                )
        #: DOCUMENT ME
        dims = MPI.Compute_dims(self.comm.size, (0,0)) # returns a balanced distribtion of processors
        P1 = dims[0] # CPUs in 1st direction
        P2 = dims[1] # CPUs in 2nd direction
        # Create two communicator groups (?) 
        self.comm_zy = self.comm.Split(self.comm.rank % P1)
        self.comm_yx = self.comm.Split(self.comm.rank // P1)
        
        #: DOCUMENT ME
        x_slices = self.decompose(self.pdims[0], P1, even=True)
        y_slices = self.decompose(self.pdims[1], P2, even=True)

        # x_slices = [
        #     slice(i*self.pdims[0]//P1,
        #           (i+1)*self.pdims[0]//P1)
        #     for i in range(P1)
        #     ]
        # y_slices = [
        #     slice(i*self.pdims[0]//P2,
        #           (i+1)*self.pdims[0]//P2)
        #     for i in range(P2)
        #     ]
        #if self.comm.rank==0:
        #    for s in x_slices:
        #        print('x', s)
        #    for s in y_slices:
        #        print('y', s)

        #: A list, each element of which is a 3-tuple of
        #: Python :class:`slice` objects describing the slice of the
        #: global physical mesh stored on the n-th MPI rank
        #self.physical_slices = [
        #    (slice(i*self.pdims[0]//self.comm.size,
        #           (i+1)*self.pdims[0]//self.comm.size),
        #     slice(0, self.pdims[1]),
        #     slice(0, self.pdims[2]))
        #    for i in range(self.comm.size)
        #    ]
        #: The slice of the global physical mesh stored by this process

        # Get local ranks. Due to the communicator split, we have more "local" slices
        zy_rank = self.comm_zy.Get_rank()
        yx_rank = self.comm_yx.Get_rank()
        self._local_x_slice = x_slices[yx_rank]
        self._local_y_slice = y_slices[zy_rank]

        local_physical_slice = (
            self._local_x_slice,
            self._local_y_slice,
            slice(0, self.pdims[2])
            )
        
        #self.local_physical_slice = self.physical_slices[self.comm.rank]
        #: A list, each element of which is a 3-tuple of
        #: Python :class:`slice` objects describing the slice of the
        #: global spectral mesh stored on the n-th MPI rank
        #:
        #: We require the number of MPI ranks to be even, so that we do
        #: not have to deal with the case where a spectral slice includes
        #: two discontinous subarrays, since the associated MPI datatype
        #: is likely to be inefficient.
        #:
        #: The domain is decomposed into two parts, each using half of the
        #: MPI ranks.  The first contain the zero mode and positive modes,
        #: the second contains the negative modes.  This decomposition will
        #: not be optimally efficient for certain cases, such as when
        #: there are two ranks (one will have two more modes than the other),
        #: or when the number of ranks is equal to the number of modes (the
        #: decomposition will fail, whereas it should allocate two additional
        #: ranks to the first half of the mode space).
        
        #self.spectral_slices = [
        #    (slice(0, self.sdims[0]),
        #     slice(i*self.sdims[1]//self.comm.size,
        #           (i+1)*self.sdims[1]//self.comm.size),
        #     slice(0, self.sdims[2]//2+1))
        #    for i in range(self.comm.size)
        #    ]
        
        # modified so that the lengths of the slices are in the same order as the dealias_slices
        # currently only for odd sdims and even comm.size 
        #q, r = divmod(self.sdims[1], self.comm.size)
        #self.spectral_slices = [
        #    (slice(0, self.sdims[0]),
        #     slice(i*q + min(i,r), (i+1)*q + min(i+1,r)),
        #     slice(0, self.sdims[2]//2+1))
        #    for i in range(self.comm.size) 
        #    ]

        #if self.comm.size % 2:
        #    raise ValueError("Number of MPI ranks must be even.")
        ky_slices = self.decompose(self.sdims[1], P1)
        kz_slices = self.decompose(self.sdims[2] // 2 + 1, P2, even=True)

        # if self.comm.rank == 0:
        #     print(P1, P2)
        #     print(x_slices)
        #     print(y_slices)
        #     print(ky_slices)
        #     print(kz_slices)
        
        #: The slice of the global spectral mesh stored by this process
        # loal on comm_zy
        self._local_ky_slice = ky_slices[yx_rank]
        self._local_kz_slice = kz_slices[zy_rank]

        #if self.comm.rank==0:
        #    for s in ky_slices:
        #        print('ky', s)
        #    for s in kz_slices:
        #        print('kz', s)

        local_spectral_slice = (
            slice(0, self.sdims[0]),
            self._local_ky_slice,
            self._local_kz_slice
            )
        #: A 3-tuple with the physical mesh spacing in each dimension
        self.dx = box_size/self.pdims
        #: The local physical space mesh
        self.x = self.dx[:,numpy.newaxis, numpy.newaxis, numpy.newaxis] \
            * numpy.mgrid[local_physical_slice]
        k = numpy.mgrid[local_spectral_slice]
        # Note, use sample spacing/2pi to get radial frequencies, rather
        # than circular frequencies.
        fftfreq0 = numpy.fft.fftfreq(self.pdims[0], self.dx[0]/(2*numpy.pi))[
            [*range(0, (self.sdims[0]+1)//2), *range(-(self.sdims[0]//2), 0)]
            ]
        fftfreq1 = numpy.fft.fftfreq(self.pdims[1], self.dx[1]/(2*numpy.pi))[
            [*range(0, (self.sdims[1]+1)//2), *range(-(self.sdims[1]//2), 0)]
            ]
        rfftfreq = numpy.fft.rfftfreq(
            self.pdims[2],self.dx[2]/(2*numpy.pi)
            )[:self.sdims[2]//2+1]
        #: The local spectral space mesh (wavenumbers)
        #print(self.comm.rank, local_spectral_slice, fftfreq0.shape, fftfreq1.shape, rfftfreq.shape)
	
        self.k = numpy.array([
            fftfreq0[k[0]],
            fftfreq1[k[1]],
            rfftfreq[k[2]]
            ])
        #: The local wavenumber magnitude squared.
        self.k2 = numpy.sum(self.k*self.k, axis=0)
        #: The local wavenumber magnitude.
        self.kmag = numpy.sqrt(self.k2)
        #: The global maximum wavenumber magnitude.
        self.kmax = numpy.sqrt(
            numpy.amax(fftfreq0**2) + numpy.amax(fftfreq1**2)
            + numpy.amax(rfftfreq**2)
            )
        
        s1 = self.sdims[1] // 2 + 1
        aliased_size = self.pdims[1] - self.sdims[1]        
        #: Meshes are domain decomposed into slabs in the first dimension
        #: in physical space, and the second dimension in spectral
        #: space.  A three-dimensional FFT is performed by first
        #: performing a one-dimensional FFT along the first axis, and
        #: then using an MPI Alltoall communication to swap the slab
        #: decomposition, and finally perfomring a two-dimensional FFT
        #: along the remaing axes.  (That is for a spectral-to-physical
        #: transformation.  The physical-to-spectral transformation is
        #: done by reversing the procedure.)  :attr:`slice1` is a list,
        #: each element of which is an MPI type describing the sub-array
        #: on the current process that will be moved to the n-th rank
        #: during the Alltoall communication.  :attr:`slice2` are the
        #: corresponding receiving slices (i.e, the n-th element
        #: describes the data to be received from the n-th rank to the
        #: current rank).
        #self.slice1 = [
        #    MPI.DOUBLE_COMPLEX.Create_subarray(
        #        [self._local_x_slice.stop
        #         - self._local_x_slice.start,
        #         self.pdims[1],
        #         self.pdims[2]//2+1
        #        ],
        #        [self._local_x_slice.stop
        #         - self._local_x_slice.start,
        #         s.stop - s.start,
        #         self.sdims[2]//2+1
        #        ],
        #        [0, s.start + (aliased_size if s.start >= s1 else 0), 0],
        #        ).Commit()
        #    for s in ky_slices
        #    ]
        #: A list of receiving data types for the slab decomposition
        #: swap (see :attr:`slice1`).
        #self.slice2 = [
        #    MPI.DOUBLE_COMPLEX.Create_subarray(
        #        [self.pdims[0],
        #         self._local_ky_slice.stop
        #         - self._local_ky_slice.start,
        #         self.sdims[2]//2+1
        #        ],
        #        [p.stop - p.start,
        #         self._local_ky_slice.stop
        #         - self._local_ky_slice.start,
        #         self.sdims[2]//2+1
        #        ],
        #        [p.start, 0, 0],
        #        ).Commit()
        #    for p in x_slices
        #    ]


        #for s in kz_slices:
        #    print(self.comm.rank, [self._local_x_slice.stop 
        #         - self._local_x_slice.start,
        #         self._local_y_slice.stop 
        #         - self._local_y_slice.start,
        #         self.pdims[2]//2 + 1
        #         ])

        #: DOCUMENT ME
        self._xy_pencils = [
            MPI.DOUBLE_COMPLEX.Create_subarray(
                [self._local_x_slice.stop 
                 - self._local_x_slice.start,
                 self._local_y_slice.stop 
                 - self._local_y_slice.start,
                 self.pdims[2]//2 + 1
                ],
                [self._local_x_slice.stop 
                 - self._local_x_slice.start,
                 self._local_y_slice.stop 
                 - self._local_y_slice.start,
                 s.stop - s.start
                ],
                [0, 0, s.start]
                ).Commit()
            for s in kz_slices
            ]
        self._xkz_pencils = [
            MPI.DOUBLE_COMPLEX.Create_subarray(
                [self._local_x_slice.stop 
                 - self._local_x_slice.start,
                 self.pdims[1],                
                 self._local_kz_slice.stop
                 - self._local_kz_slice.start
                ],
                [self._local_x_slice.stop
                 - self._local_x_slice.start,
                 p.stop - p.start,
                 self._local_kz_slice.stop
                 - self._local_kz_slice.start
                ],
                [0, p.start, 0]
                ).Commit()
            for p in y_slices
            ]
        self._xkz2_pencils = [
            MPI.DOUBLE_COMPLEX.Create_subarray(
                [self._local_x_slice.stop 
                 - self._local_x_slice.start,
                 self.pdims[1],
                 self._local_kz_slice.stop
                 - self._local_kz_slice.start
                ],
                [self._local_x_slice.stop 
                 - self._local_x_slice.start,
                 s.stop - s.start,
                 self._local_kz_slice.stop
                 - self._local_kz_slice.start
                ],
                [0, s.start + (aliased_size if s.start >= s1 else 0), 0]
                ).Commit()
            if s.stop <= s1 or s.start >= s1 or self.pdims[1] == self.sdims[1] else
            MPI.DOUBLE_COMPLEX.Create_indexed(
                [ self._local_kz_slice.stop - self._local_kz_slice.start ],
                [ 0 ],
                ).Create_resized(0, (self._local_kz_slice.stop - self._local_kz_slice.start)*16).Create_indexed(
                    [ s1 - s.start, s.stop - s1 ],
                    [ s.start, s1 + aliased_size ],
                    ).Create_resized(0, self.pdims[1]*(self._local_kz_slice.stop - self._local_kz_slice.start)*16).Create_indexed(
                        [ self._local_x_slice.stop - self._local_x_slice.start ],
                        [ 0 ]
                        ).Commit()
            for s in ky_slices
            ]
        self._kykz_pencils = [
            MPI.DOUBLE_COMPLEX.Create_subarray(
                [self.pdims[0],
                 self._local_ky_slice.stop
                 - self._local_ky_slice.start, 
                 self._local_kz_slice.stop 
                 - self._local_kz_slice.start
                ],
                [p.stop - p.start,
                 self._local_ky_slice.stop
                 - self._local_ky_slice.start, 
                 self._local_kz_slice.stop 
                 - self._local_kz_slice.start
                ],
                [p.start, 0, 0]
                ).Commit()
            for p in x_slices
            ]

    def __del__(self):
        self.comm_zy.Free()
        self.comm_yx.Free()

              
    @cached_property
    def P(self):
        r"""Navier-Stokes pressure projection operator.

        The pressure projection operator used in the spectral form of
        the incompressible Navier-Stokes equations,

        .. math::

            P_{ij} = \delta_{ij} - \frac{k_i k_j}{k^2}

        where :math:`k_i` is the wavevector.

        Since this is only used by Navier-Stokes operators, it is
        implemented as a cached property.
        """
        return (numpy.eye(3)[:, :, None, None, None]
                - self.k[None, ...] * self.k[:, None, ...]
                / numpy.where(self.k2 == 0, 1, self.k2))

    @staticmethod
    def decompose(nmodes, ncpus, even=True):
        """Decompose *nmodes* across *ncpus*."""
        if even:
            return [
                slice(i*nmodes//ncpus, (i+1)*nmodes//ncpus)
                for i in range(ncpus)
                ]
        else:
            # Note, this may be too much work.  Just divide ncpus in two, and then handle the specuial
            # case of ncpus == nmodes if necessary.  This is already suboptimal for large odd ncpus.
            if ncpus == 1:
                raise ValueError("Even decomposition is impossible across one processor")
            nmodes1 = nmodes // 2 + 1
            nmodes2 = ( nmodes - 1 ) // 2
            ncpus1 = ncpus * nmodes1 // nmodes
            ncpus2 = ncpus - ncpus1
            return [
                slice(i*nmodes1//ncpus1, (i+1)*nmodes1//ncpus1)
                for i in range(ncpus1)
                ] + [
                slice(i*nmodes2//ncpus2 + nmodes1, (i+1)*nmodes2//ncpus2 + nmodes1)
                for i in range(ncpus2)
                ]
                
            
class PhysicalArray(numpy.lib.mixins.NDArrayOperatorsMixin):
    """Array of physical space data

    A :class:`PhysicalArray` is a specialized distributed array class
    that holds a tensor data field in its physical space representation.
    """
    def __init__(self, grid, shape_or_data=(), dtype=float):
        """Create a new physical space distributed array

        There are two ways to create a :class:`PhysicalArray`.  In both
        cases, the user must provide a *grid* of type
        :class:`SpectralGrid`.  To create a new, empty array, the
        optional *shape_or_data* argument should be a tuple with the
        shape of the data tensor.  So ``shape_or_data=()`` (the default)
        will result in a scalar array, ``shape_or_data=(3,)`` a vector
        of length 3, etc.  A data type can be provided in the *dtype*
        argument.

        For example, the call::

            p = PhysicalArray(SpectralGrid(8), (3,))

        will return a new data object with ``p.shape == (3,8,8,8)``
        (when run on a single MPI rank).

        In the second method, *shape_or_data* is an existing data array,
        or any type that can be converted to a :class:`numpy.ndarray`,
        which is used for the data field, and *dtype* is ignored.  In
        this case, the shape of the data passed must be consistent with
        the *grid*.  That is, the last three dimensions of
        *shape_or_data* must have the same shape as the local (not
        global) physical grid, and any remaining leading dimensions are
        used to determine the dimensionality of the tensor data.

        For a single MPI rank, the following is equivalent to the
        previous example::

            p = PhysicalArray(SpectralGrid(8), numpy.zeros((3,8,8,8))

        With more than one MPI rank, this code will not work, because
        the passed data is sized to the global mesh, not the local
        mesh.  Normally, users will use the first method, and the second
        is for internal use within the :mod:`psdns` package.
        """
        try:
            if shape_or_data.shape[-3:] != grid.x.shape[1:]:
                raise ValueError(
                    "data array shape {} does not match grid shape {}".format(
                        shape_or_data.shape[-3:], grid.x.shape[1:]
                        )
                    )
            self._data = numpy.asarray(shape_or_data)
        except AttributeError:
            self._data = numpy.zeros(
                shape=list(shape_or_data)+list(grid.x.shape[1:]),
                dtype=dtype,
                )
        #: The :class:`SpectralGrid` describing the data
        self.grid = grid
        #: The shape of the local data array
        self.shape = self._data.shape

    def __array__(self, dtype=None):
        """Return a view of the data as an :class:`numpy.ndarray`."""
        return numpy.array(self._data, dtype, copy=False)

    def __str__(self):
        return str(self._data)

    def __repr__(self):
        return f"PhysicalArray({str(self._data)})"

    def __getitem__(self, key):
        # Need to add checks for valid extents.  Specifically, for
        # slices check whether the return type should be a Physical
        # array or a regular numpy.ndarray.
        try:
            ret = PhysicalArray(self.grid, self._data[key])
        except ValueError:
            ret = self._data[key]
        return ret

    def __setitem__(self, key, value):
        self._data[key] = value

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        inputs = [i._data if isinstance(i, PhysicalArray) else i
                  for i in inputs]
        if 'out' in kwargs:
            kwargs['out'] = tuple(
                i._data if isinstance(i, SpectralArray)
                else i
                for i in kwargs['out']
                )
        ret = getattr(ufunc, method)(*inputs, **kwargs)
        if method == '__call__':
            # Need additional type and shape checking to see when to
            # return a PhysicalArray, when an ndarray, and when the
            # operation is invalid.
            return PhysicalArray(self.grid, ret)
        else:
            return ret

    def transpose(self, *indicies):
        """Returns a view of the array with axes transposed

        This method returns a new view of the array with the axes
        transposed.  For a rank-2 tensor, ``p.transpose()`` returns the
        standard matrix transpose.  For higher rank tensors, the
        *indicies* behave the same as for the
        :meth:`numpy.ndarray.transpose` method.  Note that transposing
        any of the last three dimensions does not make sense.
        """
        return PhysicalArray(self.grid, self._data.transpose(*indicies))

    def clip(self, min=None, max=None):
        """Returns a copy of the array limited to the specified range

        This method returns a copy of the array with the data clipped to
        the range specified by *min* and *max*.
        """
        return PhysicalArray(self.grid, self._data.clip(min, max))
 
    def to_spectral(self):
        """Transform to spectral space.

        This method returns a :class:`SpectralArray` containing the
        Fourier transform of the data.
        """
        # Index array which picks out retained modes in a complex transform
        N = self.grid.sdims
        M = self.grid.pdims
        i0 = numpy.array([*range(0, N[0]//2+1), *range(-((N[0]-1)//2), 0)])
        i1 = numpy.array([*range(0, N[1]//2+1), *range(-((N[1]-1)//2), 0)])
        # It would be more efficient to design MPI slices that fit
        # the non-contiguous array returned by the slicing here.
        #t1 = numpy.fft.rfft2(self._data)
        t1 = numpy.fft.rfft(
                 self._data,
                 axis=-1
                 )
        t1 = numpy.ascontiguousarray(t1)
        t2 = numpy.zeros(
            t1.shape[:-3]
            + (self.grid._local_x_slice.stop
               - self.grid._local_x_slice.start,
               self.grid.pdims[1], 
               self.grid._local_kz_slice.stop
               - self.grid._local_kz_slice.start),
            dtype=complex
            )
        # Note:
        # 1. The length of count and disp correspond to the number of slabs we
        #    are transferring between (which is the MPI rank).
        # 2. The value of count is the size of the tensor (which is the same
        #    for all slabs.
        # 3. The value of disp is the offset of the portion of the array we are
        #    transferring.  Since the MPI datatype is a subarray, the offset is
        #    always zero, with the datatype itself containing the true offset.
        # 4. The offset between each of the count items to be transferred,
        #    which is *count*, is also already embedded in the subarray MPI
        #    datatype.
        count = numpy.prod(self.shape[:-3], dtype=int)
        counts = [count] * self.grid.comm_zy.size
        disp = [0] * self.grid.comm_zy.size

        self.grid.comm_zy.Alltoallw(
            [t1, counts, disp, self.grid._xy_pencils],
            [t2, counts, disp, self.grid._xkz_pencils]
            )
        t3 = numpy.fft.fft(
                 t2,
                 axis=-2
                 )
        if self.grid._aliasing_strategy == 'mpi4py':
            if M[1] > N[1] and N[1] % 2 == 0:
                t3[..., :, N[1]//2, :] = \
                  t3[..., :, N[1]//2, :] + t3[..., :, -(N[1]//2), :]
        elif self.grid._aliasing_strategy == 'truncate':
            if M[1] > N[1] and N[1] % 2 == 0:
                t3[..., :, N[1]//2, :] = 0
        t3 = numpy.ascontiguousarray(t3)
        t4 = numpy.zeros(
            t3.shape[:-3]
            + (self.grid.pdims[0],
               self.grid._local_ky_slice.stop
               - self.grid._local_ky_slice.start, 
               self.grid._local_kz_slice.stop 
               - self.grid._local_kz_slice.start),
            dtype=complex
            )
        count = numpy.prod(self.shape[:-3], dtype=int)
        counts = [count] * self.grid.comm_yx.size
        disp = [0] * self.grid.comm_yx.size

        #print('t3', t3.shape, 't4', t4.shape)

        #print(self.grid.comm.rank, self.grid._xkz2_pencils, self.grid._kykz_pencils, flush=True)

        self.grid.comm_yx.Alltoallw(
            [t3, counts, disp, self.grid._xkz2_pencils],
            [t4, counts, disp, self.grid._kykz_pencils]
            )

        t5 = numpy.fft.fft(
            t4,
            axis=-3,
            )
        if self.grid._aliasing_strategy == 'mpi4py':
            if M[0] > N[0] and N[0] % 2 == 0:
                t5[..., N[0]//2, :, :] = \
                  t5[..., N[0]//2, :, :] + t5[..., -(N[0]//2), :, :]
        if self.grid._aliasing_strategy == 'truncate':
            if M[0] > N[0] and N[0] % 2 == 0:
                t5[..., N[0]//2, :, :] = 0
        t5 = t5[..., i0, :, :]/numpy.prod(self.grid.pdims)
        return SpectralArray(self.grid, t5)

    def norm(self):
        """Return the :math:`L_2` norm of the data."""
        n = self.grid.comm.reduce(numpy.sum(self*self))
        if self.grid.comm.rank == 0:
            n /= numpy.product(self.grid.pdims)
        return n

    def average(self):
        """Return the average of the data."""
        n = self.grid.comm.reduce(numpy.sum(self))
        if self.grid.comm.rank == 0:
            n /= numpy.product(self.grid.pdims)
        return n


class SpectralArray(numpy.lib.mixins.NDArrayOperatorsMixin):
    """Array of spectral space data

    A :class:`SpectralArray` is a specialized distributed array class
    that holds a tensor data field in its spectral space representation.
    """
    def __init__(self, grid, shape_or_data=(), dtype=complex):
        """Create a new physical space distributed array

        :class:`SpectralArray` supports the same two methods of object
        creation as :class:`PhysicalArray`, with the differences that
        the data shape will be be determined by the local spectral
        dimensions, and the default data type is complex.

        So, for example, the normal way to create a
        :class:`SpectralArray` would be::

            s = SpectralArray(SpectralGrid(8), (3,))

        which, on a single MPI rank, will return a new data object with
        ``s.shape == (3,8,8,5)``.
        """
        try:
            if shape_or_data.shape[-3:] != grid.k.shape[1:]:
                raise ValueError(
                    "data array shape {} does not match grid shape {}".format(
                        shape_or_data.shape[-3:], grid.k.shape[1:]
                        )
                    )
            self._data = numpy.asarray(shape_or_data)
        except AttributeError:
            self._data = numpy.zeros(
                shape=list(shape_or_data)+list(grid.k.shape[1:]),
                dtype=dtype
                )
        #: The :class:`SpectralGrid` describing the data
        self.grid = grid
        #: The shape of the local data array
        self.shape = self._data.shape

    def __array__(self, dtype=None):
        return numpy.array(self._data, dtype, copy=False)

    def __str__(self):
        return str(self._data)

    def __repr__(self):
        return f"SpectralArray({str(self._data)})"

    def __getitem__(self, key):
        try:
            ret = SpectralArray(self.grid, self._data[key])
        except ValueError:
            ret = self._data[key]
        return ret

    def __setitem__(self, key, value):
        self._data[key] = value

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        if method == '__call__':
            # Need additional type and shape checking to see when to
            # return a PhysicalArray, when an ndarray, and when the
            # operation is invalid.
            # There should be a cleaner way to do the following:
            if 'out' in kwargs:
                kwargs['out'] = tuple(
                    i._data if isinstance(i, SpectralArray)
                    else i
                    for i in kwargs['out']
                    )
            return SpectralArray(
                self.grid,
                ufunc(
                    *[i._data if isinstance(i, SpectralArray) else i
                      for i in inputs],
                    **kwargs
                    ),
                )
        else:
            return NotImplemented

    def mpi_get_view(self):
        """Return MPI file view

        Return a MPI data type which represents the view of the local
        spectral data in a MPI data file.
        """
        return MPI.DOUBLE_COMPLEX.Create_subarray(
            [ self.grid.sdims[0],
              self.grid.sdims[1],
              self.grid.sdims[2]//2+1 ],
            [ self.grid.sdims[0],
              self.grid._local_ky_slice.stop - self.grid._local_ky_slice.start,
              self.grid._local_kz_slice.stop - self.grid._local_kz_slice.start ],
            [ 0,
              self.grid._local_ky_slice.start,
              self.grid._local_kz_slice.start ]
            ).Commit()

    def checkpoint(self, filename):
        """Write a checkpoint file

        Use MPI parallel file routines to write the
        :class:`SpectralData` to a file named *filename*.
        """
        view = self.mpi_get_view()
        fh = MPI.File.Open(
            self.grid.comm, filename, MPI.MODE_WRONLY | MPI.MODE_CREATE
            )
        fh.Set_view(0, filetype=view)
        fh.Write_all(self._data)
        fh.Close()
        view.Free()
        return self
    
    def read_checkpoint(self, filename):
        """Read a checkpoint file

        Use MPI parallel file routines to read the
        :class:`SpectralData` from a file named *filename*.
        """
        view = self.mpi_get_view()
        fh = MPI.File.Open(self.grid.comm, filename, MPI.MODE_RDONLY)
        fh.Set_view(0, filetype=view)
        fh.Read_all(self._data)
        fh.Close()
        view.Free()
        return self

    def copy(self):
        """Return a copy of the array"""
        return SpectralArray(self.grid, self._data.copy())

    def conjugate(self):
        """Return a new array containing the complex conjugate of the data"""
        return SpectralArray(self.grid, self._data.conjugate())

    def real(self):
        """Return a new array containing the real part of the data"""
        return self._data.real

    def transpose(self, *indicies):
        """Returns a view of the array with axes transposed

        This method returns a new view of the array with the axes
        transposed.  For a rank-2 tensor, ``p.transpose()`` returns the
        standard matrix transpose.  For higher rank tensors, the
        *indicies* behave the same as for the
        :meth:`numpy.ndarray.transpose` method.  Note that transposing
        any of the last three dimensions does not make sense.
        """
        return SpectralArray(self.grid, self._data.transpose(*indicies))

    def to_physical(self):
        """Transform to physical space.

        This method returns a :class:`PhysicalArray` containing the
        Fourier transform of the data.
        """
        N = self.grid.sdims
        M = self.grid.pdims
        i0 = numpy.array([*range(0, N[0]//2+1), *range(-((N[0]-1)//2), 0)])
        i1 = numpy.array([*range(0, N[1]//2+1), *range(-((N[1]-1)//2), 0)])
        s = numpy.zeros(
            self.shape[:-3]
            + (self.grid.pdims[0], 
               self.grid._local_ky_slice.stop
               - self.grid._local_ky_slice.start, 
               self.grid._local_kz_slice.stop
               - self.grid._local_kz_slice.start),
            dtype=complex
            )
        s[..., i0, :, :] = self
        if self.grid._aliasing_strategy == 'mpi4py':
            if M[0] > N[0] and N[0] % 2 == 0:
                s[..., N[0]//2, :, :] *= 0.5
                s[..., -(N[0]//2), :, :] = s[..., N[0]//2, :, :]
        t1 = numpy.ascontiguousarray(numpy.fft.ifft(s, axis=-3))
        #print(self.grid.comm.rank, "Completed first transform", flush=True)
        t2 = numpy.zeros(
            self.shape[:-3]
            + (self.grid._local_x_slice.stop
               - self.grid._local_x_slice.start,
               self.grid.pdims[1],
               self.grid._local_kz_slice.stop
               - self.grid._local_kz_slice.start),
            dtype=complex
            )
        count = numpy.prod(self.shape[:-3], dtype=int)
        counts = [count] * self.grid.comm_yx.size
        displs = [0] * self.grid.comm_yx.size
        self.grid.comm_yx.Alltoallw(
            [t1, counts, displs, self.grid._kykz_pencils],
            [t2, counts, displs, self.grid._xkz2_pencils],
            )
        if self.grid._aliasing_strategy == 'mpi4py':
            if M[1] > N[1] and N[1] % 2 == 0:
                t2[..., :, N[1]//2, :] *= 0.5
                t2[..., :, -(N[1]//2), :] = t2[..., :, N[1]//2, :]
        t3 = numpy.ascontiguousarray(numpy.fft.ifft(
            t2,
            axis=-2
            ))
        t4 = numpy.zeros(
            self.shape[:-3]
            + (self.grid._local_x_slice.stop
               - self.grid._local_x_slice.start,
               self.grid._local_y_slice.stop
               - self.grid._local_y_slice.start,
               self.grid.pdims[2] // 2 + 1),
            dtype=complex
            )
        count = numpy.prod(self.shape[:-3], dtype=int)
        counts = [count] * self.grid.comm_zy.size
        displs = [0] * self.grid.comm_zy.size
        self.grid.comm_zy.Alltoallw(
            [t3, counts, displs, self.grid._xkz_pencils],
            [t4, counts, displs, self.grid._xy_pencils],
            )
        t5 = numpy.fft.irfft(
            t4,
            n=self.grid.x.shape[3],
            axis=-1
            )*numpy.prod(self.grid.pdims)
        return PhysicalArray(self.grid, t5)

    def grad(self):
        """Return a new array containing the gradient of the data

        This method returns a new array with the gradient of the data.
        The operation increases the number of dimensions by one, so if
        the source array has the shape ``[i,j,...,kx,ky,kz]``, the
        returned array has the shape ``[i,j,...,3,kx,ky,kz]``.
        """
        return 1j*self[..., numpy.newaxis, :, :, :]*self.grid.k

    def div(self):
        """Return a new array containing the divergence of the data

        This method returns a new array with the divergence of the data
        along the first axis of the data.  The opeartion decreases the
        number of dimensions by one, so if the source array has the
        shape ``[3,i,j,...,kx,ky,kz]``, the returned array will have the
        shape ``[i,j,...,kx,ky,kz]``.

        """
        return SpectralArray(
            self.grid,
            1j*numpy.einsum("i...,i...->...", self.grid.k, self),
            )

    def curl(self):
        """Return the curl of the data.
        """
        return SpectralArray(
            self.grid,
            1j*numpy.cross(self.grid.k, self, axis=0),
            )

    def norm(self): #L2 norm squared
        """Return the :math:`L_2` norm of the data
        """
        w = 2*numpy.ones(
            self.grid._local_kz_slice.stop
            - self.grid._local_kz_slice.start,
            dtype=int
            )

        if self.grid._local_kz_slice.start == 0:
            w[0] = 1
        
        if (self.grid.pdims[2] == self.grid.sdims[2]        # No aliasing
            and self.grid.pdims[2] % 2 == 0                 # Even number of modes
            and self.grid._local_kz_slice.stop == self.grid.sdims[2] // 2 + 1):
            w[-1] = 1

        return self.grid.comm.reduce(
                numpy.sum(
                    + numpy.sum(
                        w*(self*numpy.conjugate(self)).real(),
                        axis=-1
                        )
                    )
                )

    def set_mode(self, mode, val):
        """Set a mode based on global array indicies.

        Indexing for the :class:`SpectralGrid` class is relative to the
        locally distributed array data.  Sometimes it is necessary to
        set a specific mode in global mode space, the :meth:`set_mode`
        method is provided for this purpose.  This method sets the
        global *mode* (a 3-tuple of indicies) in wavenumber space to the
        specified *val*.
        """
        mode[1] %= self.grid.sdims[1]
        if (mode[1] >= self.grid._local_ky_slice.start and
                mode[1] < self.grid._local_ky_slice.stop and 
                mode[2] >= self.grid._local_kz_slice.start and
                mode[2] < self.grid._local_kz_slice.stop):
            self._data[
                mode[0],
                mode[1]-self.grid._local_ky_slice.start,
                mode[2]-self.grid._local_kz_slice.start
                ] = val

    def get_mode(self, mode):
        """Get a mode based on global array indicies.

        This method returns the value of the global *mode* (a 3-tuple of
        indicies) in wavenumber space.
        """
        # The following is a hack, but it makes communication
        # simpler.  A cleaner approach would to be to have the
        # processor that finds the mode send the data to the root,
        # instead of a global reduce.
        mode[1] %= self.grid.sdims[1]
        if (mode[1] >= self.grid._local_ky_slice.start and
                mode[1] < self.grid._local_ky_slice.stop and 
                mode[2] >= self.grid._local_kz_slice.start and
                mode[2] < self.grid._local_kz_slice.stop):
            val = self._data[
                ...,
                mode[0],
                mode[1]-self.grid._local_ky_slice.start,
                mode[2]-self.grid._local_kz_slice.start
                ]
        else:
            val = 0
        return self.grid.comm.reduce(val)
