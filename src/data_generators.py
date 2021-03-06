from os.path import join 
import h5py
import numpy as np
from pandas import read_csv
import logging
from time import time


class DataGenerator:
    '''
    DataGenerator class
    '''
    def __init__(self, params, validate=False, verbose=True): 
        '''
        Initialisation
        Arguments:
            - params -- parameters, hparams.HParamsFromYAML
            - validate -- whether split the dataset into train and validation sets, bool.
                Defaults to False
            - verbose -- if True, print the output to console, bool
        '''
        self.hdf5_path = join(params.storage_dir, params.features_dir, params.features_type, 'train.h5')
        self.input_frames_number = params.input_frames_number
        self.validate = validate
        self.verbose = verbose
        self.batch_size = params.batch_size
        self.eval_audios_number = params.eval_audios_number
        # self.seed = params.seed

        self.train_rand_generator = np.random.RandomState(seed=params.seed)
        # use different seed to shuffle the data during validation
        self.validation_rand_generator = np.random.RandomState(seed=0)

        # load train.h5 file
        start_time = time()
        logging.info('Loading data from\n|{}...'.format(self.hdf5_path))
        hdf5 = h5py.File(self.hdf5_path, 'r')

        labels = hdf5['label'][:]
        filenames = hdf5['filename'][:]

        self.labels = sorted({s.decode() for s in labels})
        # or load labels from josn file
        self.label_index_dict = {key: i for i, key in enumerate(self.labels)}
        self.filenames = [s.decode() for s in filenames]
        self.x = hdf5['feature'][:]  # features
        self.y = np.array([self.label_index_dict[s.decode()] for s in labels])
        self.manually_verified = hdf5['manually_verified'][:]
        self.begin_end_indices = hdf5['begin_end_ind'][:]
        self.files_number = len(filenames)

        hdf5.close()
        logging.info('Loading completed successfully.\n|'
                'Elapsed time: {:.6f} s'.format(time() - start_time))

        if validate:
            logging.info('Generating train and validation indices for audio files...')
            folds = read_csv(join(params.storage_dir, params.validation_dir,
                    params.validation_meta_file), usecols=['fold'])['fold']
            self.train_audio_indices = folds.index[folds != params.holdout_fold].to_numpy()
            self.validation_audio_indices = folds.index[folds == params.holdout_fold].to_numpy()
            logging.info('Generating completed successfully')
        else:
            self.train_audio_indices = np.arange(self.files_number)
            self.validation_audio_indices = np.empty(0)
        self.train_audio_indices_len = len(self.train_audio_indices)
        self.validation_audio_indices_len = len(self.validation_audio_indices)
        
        if verbose:
            print('|------------------------------------------------------------------------------')
        logging.info('Number of audio files for training: {}'.format(self.train_audio_indices_len))
        logging.info('Number of audio files for validation: {}'.\
                format(self.validation_audio_indices_len))
        if validate:
            logging.info('Train-validation split ratio is approximately {:.6f}:1'.\
                    format(self.train_audio_indices_len / self.validation_audio_indices_len))

        train_begin_end_indices = self.begin_end_indices[self.train_audio_indices]
        x_train = [self.x[begin:end] for [begin, end] in train_begin_end_indices]

        # join along axis 0 such that the resulting array will have shape (*, [f]_number),
        # where f mean log_mel, or mfcc, or chroma
        x_train = np.concatenate(x_train, axis=0)
        # print(x_train.shape)
        # exit()

        # compute mean and std for training data, the resulting arrays will have shape ([f]_number,)
        axis = 0 if x_train.ndim == 2 else (0, 1)
        self.mean = np.mean(x_train, axis=axis)
        self.std = np.std(x_train, axis=axis)

        # generate training chunks
        self.train_chunks = []
        for i in range(self.train_audio_indices_len):
            [begin, end] = train_begin_end_indices[i]
            audio_label_ind = self.y[self.train_audio_indices[i]]
            self.train_chunks += \
                self.__generate_chunks(begin, end, audio_label_ind)
        self.train_chunks_len = len(self.train_chunks)
        
        logging.info('Number of chunks for training: {}'.format(self.train_chunks_len))
        if verbose:
            print('|------------------------------------------------------------------------------')

    def __generate_chunks(self, begin, end, audio_label_ind):
        '''
        Method that splits frames if the number of frames is > than self.input_frames_number.
        Parameters:
            - begin -- index that indicates the beginning of audiodata in the whole bunch, int >=0
            - end -- index that indicates the end of audiodata in the whole bunch, int >=0
            - audio_label_ind -- index of audio's label int >=0, <= number of classes
        Returns:
            - list of tuples (BEGIN, END, audio_label_ind)
        '''
        if end - begin <= self.input_frames_number:
            return [(begin, end, audio_label_ind)]
        else:
            begin_indices = np.arange(begin, end - self.input_frames_number,
                    step=(self.input_frames_number // 2))
            return [(b, b + self.input_frames_number, audio_label_ind) for b in begin_indices]

    def __generate_xy_batches(self, x_data, indices):
        '''
        Method that splits frames if the number of frames is > than self.input_frames_number.
        Parameters:
            - x_data -- full train x dataset or x test dataset, numpy.ndarray
            - indices -- list of begin/end indices, list
        Returns:
            - (x_batch, y_batch) -- x and y batches both being numpy.ndarray, tuple
        '''
        x_batch, y_batch = [], []
        for (begin, end, y) in indices:
            y_batch += [y]
            x = x_data[begin:end] 
            x_batch += [x] if end - begin == self.input_frames_number else \
                    [np.tile(x, (self.input_frames_number//len(x)+1, 1))[0:self.input_frames_number]]
                    # ^ repeat if the number of frames is smaller than input_frames_number
        return np.array(x_batch), np.array(y_batch)

    def train_generator(self):
        '''
        Generates batches for training using generator object and yield
        Parameters:
            - None
        Returns:
            - generator object that yields a tuple of x and y batches both being numpy arrays
        '''
        train_chunks_copy = self.train_chunks.copy()
        self.train_rand_generator.shuffle(train_chunks_copy)

        self.epoch_len = self.train_chunks_len // self.batch_size + 1
        self.epoch = 1
        logging.info('Batch size: {}'.format(self.batch_size))
        logging.info('One epoch lasts {} iterations'.format(self.epoch_len))
        if self.verbose:
            print('|------------------------------------------------------------------------------')

        batch_begin_ind = 0
        while True:
            # set batch_begin_ind to 0 and reshuffled data at every epoch
            if batch_begin_ind >= self.train_chunks_len:
                batch_begin_ind = 0
                self.train_rand_generator.shuffle(train_chunks_copy)
                self.epoch += 1
            batch_indices = train_chunks_copy[batch_begin_ind:batch_begin_ind+self.batch_size]

            # generate x, y batches
            (x_batch, y_batch) = self.__generate_xy_batches(self.x, batch_indices)
            # scale x data
            x_batch = (x_batch - self.mean) / self.std
            # update begin index to get next batch
            batch_begin_ind += self.batch_size

            yield x_batch, y_batch

    def validation_generator(self, mode, manually_verified_only, shuffle):
        '''
        Parameters:
            - mode -- 'train' or 'validation', str
            - manually_verified_only -- use only manually verified audios for evaluation, bool
            - shuffle -- shuffle or not the validation data, bool
        Returns:
            - generator object that yields a tuple of x, y batches both being numpy arrays,
                label y (int)
        ''' 
        audio_indices = self.train_audio_indices if mode == 'train' else self.validation_audio_indices
        if manually_verified_only:
            audio_indices = audio_indices[np.where(self.manually_verified[audio_indices] == 1)[0]]
        if shuffle:
            self.validation_rand_generator.shuffle(audio_indices)
        logging.info('Number of audios used for evaluation: {}'.format(self.eval_audios_number))
        if self.verbose:
            print('|------------------------------------------------------------------------------')
        for (ind, aind) in enumerate(audio_indices):
            if ind == self.eval_audios_number:
                break
            [begin, end] = self.begin_end_indices[aind]
            y = self.y[aind]
            chunk_indices = self.__generate_chunks(begin, end, y)

            # generate x, y batches
            (x_batch, y_batch) = self.__generate_xy_batches(self.x, chunk_indices)
            # scale x data
            x_batch = (x_batch - self.mean) / self.std

            yield x_batch, y_batch, y

    def read_test_data(self):
        '''
        Reads the test data from h5 file
        Parameters:
            - None 
        Returns:
            - None
        ''' 
        self.hdf5_path_test = self.hdf5_path.replace('train', 'test')
        # load test.h5 file
        start_time = time()
        logging.info('Loading data from\n|{}...'.format(self.hdf5_path_test))
        hdf5 = h5py.File(self.hdf5_path_test, 'r')

        filenames = hdf5['filename'][:]
        labels = hdf5['label'][:]
        self.filenames_test = [s.decode() for s in filenames]
        self.x_test = hdf5['feature'][:]  # features
        self.y_test = np.array([self.label_index_dict[s.decode()] for s in labels])
        self.begin_end_indices_test = hdf5['begin_end_ind'][:]

        hdf5.close()
        logging.info('Loading completed successfully.\n|'
                'Elapsed time: {:.6f} s'.format(time() - start_time))


    def test_generator(self):
        '''
        Parameters:
            - None 
        Returns:
            - generator object that yields a tuple of x batch being a numpy array,
                and audio filename (string) and y (int) of the corresponding audio
        ''' 
        for (aind, filename) in enumerate(self.filenames_test):
            [begin, end] = self.begin_end_indices_test[aind]
            chunk_indices = self.__generate_chunks(begin, end, None)

            # generate x, y batches
            (x_batch, _) = self.__generate_xy_batches(self.x_test, chunk_indices)
            # scale test x data using the mean and std of train data
            x_batch = (x_batch - self.mean) / self.std

            yield x_batch, filename, self.y_test[aind]
