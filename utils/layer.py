import torch
from utils.fft_filter import fft_filter_2D, fft_filter_1D
from utils.style_loss import gram_matrix, BN_mean_and_std, histogram_loss, kernels, kernel_mean, mean_square_distance

# GPU
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# stabilization in case denominator is 0
eps = 1e-5

class ContentLoss(torch.nn.Module):
    """
    A special layer which is inserted into VGG network. This layer stores an input
    tensor on initialization, which is the feature map at corresponding insertion 
    position. After intialization, the ContentLoss layer is transparent to the whole 
    network on forward pass, as it returns the input as output. During forward pass, 
    this layer computes the MSE between input and its stored tensor as content loss.
    """

    def __init__(self, target_content):
        super(ContentLoss, self).__init__()
        self.target_content = target_content.detach()
         
    def forward(self, input):
        self.loss = torch.nn.functional.mse_loss(input, self.target_content)
        return input

class GetMask(torch.nn.Module):
    """
    A special layer which does nothing on initialization and stores input during
    forward pass. As it returns input as output directly, this layer is transparent
    to the whole network.
    """
    def __init__(self):
        super(GetMask, self).__init__()
    
    def forward(self, input):
        self.mask = input.detach()
        return input

class StyleLoss(torch.nn.Module):
    """
    A special layer which is inserted into VGG network. This layer stores one or multiple
    style statistics and a mask layer on initialization. After intialization, this layer 
    is transparent to the whole network on forward pass, as it returns the input as output. 
    During forward pass, this layer computes one or multiple types of style losses according
    to arguments.

    Currently, 7 types of style losses are supported. They are:
    'gram': style loss based on gram matrix
    'bnst': style loss based on batch normalization statistics
    'morest': style loss based on more statistics
    'histo': histogram matching loss
    'linear': style loss based on linear kernel
    'poly': style loss based on polynomial kernel (degree 2)
    'rbf': style loss based on rbf kernel
    """

    def __init__(self, target_style, style_loss_types, mask_layer, masking = False, fft_level = 0, freq_lower = None, freq_upper = None):
        """
        Arguments:
            target_style: style feature map tensor of shape (b,c,h,w)
            style_loss_types: a dictionary with style loss name as key and its weight as value
            mask_layer: an instance of GetMask layer
            masking: whether to apply masking or not, boolean
            fft_level: apply FFT filter on which feature level
            freq_lower: FFT high pass filter threshold
            freq_upper: FFT low pass filter threshold
        """
        super(StyleLoss, self).__init__()

        # mask
        self.mask_layer = mask_layer
        self.masking = masking

        # b: batch size, which should be 1
        # c: number of channels
        # h: height
        # w: width
        b, c, h, w = target_style.size()
        # print("c:", "{:3d}".format(c), "  h:", "{:3d}".format(h), "  w:", "{:3d}".format(w))
        
        # FFT filter on level 1 feature
        if fft_level == 1:
            target_style = fft_filter_2D(target_style[0], freq_lower = freq_lower, freq_upper = freq_upper).unsqueeze(0)
        
        # flattened view of style feature
        style_feature = target_style.view(c, h * w)
        
        # FFT filter on level 2 feature
        if fft_level == 2:
            style_feature = fft_filter_2D(style_feature, freq_lower = freq_lower, freq_upper = freq_upper)
        
        self.use_gram   = 'gram'   in style_loss_types
        self.use_bnst   = 'bnst'   in style_loss_types
        self.use_morest = 'morest' in style_loss_types
        self.use_histo  = 'histo'  in style_loss_types
        self.use_kernel = 'linear' in style_loss_types or 'poly' in style_loss_types or 'rbf' in style_loss_types

        # gram matrix
        if self.use_gram:
            self.target_gram_matrix = gram_matrix(style_feature).detach()

            # FFT filter on level 3 feature
            if fft_level == 3:
                self.target_gram_matrix = fft_filter_2D(self.target_gram_matrix, freq_lower = freq_lower, freq_upper = freq_upper)
        
        # BN mean and std 
        if self.use_bnst:
            self.target_mean, self.target_std = BN_mean_and_std(style_feature)
            self.target_mean.detach_()
            self.target_std.detach_()

            # FFT filter on level 4 feature
            if fft_level == 4:
                self.target_mean = fft_filter_1D(self.target_mean, freq_lower = freq_lower, freq_upper = freq_upper)
                self.target_std = fft_filter_1D(self.target_std, freq_lower = freq_lower, freq_upper = freq_upper)
        
        # more statistics
        if self.use_morest:
            self.target_c_std = target_style[0].std(0)#.mean()
            self.target_h_std = target_style[0].std(1)#.mean(1)
            self.target_w_std = target_style[0].std(2)#.mean(1)
        
        # histogram
        if self.use_histo:
            # normalize features to range (0, 1)
            style_feature_normalized = (style_feature - style_feature.min(1)[0][:, None]) / (eps + style_feature.max(1)[0][:, None] - style_feature.min(1)[0][:, None])
            
            # match histogram, reference https://stackoverflow.com/questions/32655686/histogram-matching-of-two-images-in-python-2-x/33047048#33047048
            self.style_ordered = []
            self.style_quantiles = []
            for feature in style_feature_normalized:
                s_ordered, s_counts = feature.unique(return_counts=True, sorted=True)
                s_quantiles = torch.cumsum(s_counts, dim=0, dtype=torch.float)
                s_quantiles = s_quantiles / s_quantiles[-1]
                self.style_ordered.append(s_ordered.detach())
                self.style_quantiles.append(s_quantiles.detach())
        
        # kernels
        if self.use_kernel:
            # tranpose
            self.transposed_style = style_feature.transpose(0, 1).contiguous().detach()
            
            # kernel names and weights
            self.kernel_names = []
            for name in ['linear', 'poly', 'rbf']:
                if name in style_loss_types:
                    self.kernel_names.append(name)
                    
            # self.rbf_p = torch.tensor([1/((self.transposed_style**2).sum(1).mean())]).detach().to(device)


    ####################################### forward #######################################
    def forward(self, input):
        b, c, h, w = input.size()     # b = 1 since there is only one image in batch

        # masking feature
        input_masked = input * self.mask_layer.mask if self.masking else input

        input_feature = input_masked.view(c, h * w)

        # losses is a dictionary of name: value
        self.losses = {}

        # gram matrix loss
        if self.use_gram:
            input_gram_matrix = gram_matrix(input_feature)
            self.losses['gram'] = torch.nn.functional.mse_loss(input_gram_matrix, self.target_gram_matrix)
        
        # BN mean and std loss   
        if self.use_bnst:
            input_mean, input_std = BN_mean_and_std(input_feature)
            mean_loss = torch.nn.functional.mse_loss(input_mean, self.target_mean)
            std_loss  = torch.nn.functional.mse_loss(input_std, self.target_std)
            self.losses['bnst'] = mean_loss + std_loss

        # loss based on more statistics
        if self.use_morest:    
            input_feature_m = input_feature.view(c, h, w)
            input_c_std = input_feature_m.std(0)
            input_h_std = input_feature_m.std(1)
            input_w_std = input_feature_m.std(2)
            c_loss = torch.nn.functional.mse_loss(self.target_c_std, input_c_std)
            h_loss = torch.nn.functional.mse_loss(self.target_h_std, input_h_std)
            w_loss = torch.nn.functional.mse_loss(self.target_w_std, input_w_std)
            self.losses['morest'] = h_loss + w_loss + c_loss 
        
        # histogram loss
        if self.use_histo:
            # normalize features to range (0, 1)
            input_feature_normalized = (input_feature - input_feature.min(1)[0][:, None]) / (eps + input_feature.max(1)[0][:, None] - input_feature.min(1)[0][:, None])
            
            self.losses['histo'] = histogram_loss(input_feature_normalized, self.style_ordered, self.style_quantiles)
        
        # kernel loss 
        if self.use_kernel:
            # transposed input feature
            tranposed_input = input_feature.transpose(0, 1).contiguous()

            for k_name in self.kernel_names:
                p = torch.tensor([0.]).to(device) if k_name != 'rbf' else 1/torch.tensor([mean_square_distance(c, self.transposed_style, tranposed_input)]).to(device)
                self.losses[k_name] = kernel_mean(kernels[k_name][c], p, self.transposed_style, tranposed_input)/(c**2)
        
    
        return input

class StyleLossOpsOnBNST(torch.nn.Module):  
    """
    A simplified style loss layer which only computes style loss based on batch normalization statistics, 
    and allow more detailed operations on those statistics, such as affine transformation and FFT filter

    Especially, affine transformation means: x --> x * x_coef + x_bias
    """

    def __init__(self, target_style, indices = None,
                mean_coef = 1, mean_bias = 0, std_coef = 1, std_bias = 0, 
                mean_freq = [(None, None)], std_freq = [(None, None)]):
        super(StyleLossOpsOnBNST, self).__init__()

        # b: batch size, which should be 1
        # c: number of channels
        # h: height
        # w: width
        b, c, h, w = target_style.size()
        # print("c:", "{:3d}".format(c), "  h:", "{:3d}".format(h), "  w:", "{:3d}".format(w))
        style_feature = target_style.view(c, h * w)
        
        self.target_mean, self.target_std = BN_mean_and_std(style_feature)
        self.target_mean.detach_()
        self.target_std.detach_()

        # apply 1D FFT filters for mean
        self.target_mean_filtered = 0
        for freq_lower, freq_upper in mean_freq:
            self.target_mean_filtered += fft_filter_1D(self.target_mean, freq_lower = freq_lower, freq_upper = freq_upper)
        
        # apply 1D FFT filters for std
        self.target_std_filtered = 0
        for freq_lower, freq_upper in std_freq:
            self.target_std_filtered += fft_filter_1D(self.target_std, freq_lower = freq_lower, freq_upper = freq_upper)

        # affine transformation
        self.target_mean_filtered = self.target_mean_filtered * mean_coef + mean_bias # self.target_mean = self.target_mean * mean_coef + mean_bias
        self.target_std_filtered  = self.target_std_filtered  * std_coef  + std_bias # self.target_std  = self.target_std  * std_coef  + std_bias

        # indices
        self.indices = indices

    ####################################### forward #######################################
    def forward(self, input):
        b, c, h, w = input.size()     # b = 1 since there is only one image in batch
        
        input_feature = input.view(c, h * w)
 
        input_mean, input_std = BN_mean_and_std(input_feature)
        
        # consider statistics from all channels
        if self.indices is None:
            mean_loss = torch.nn.functional.mse_loss(input_mean, self.target_mean_filtered) # mean_loss = torch.nn.functional.mse_loss(input_mean, self.target_mean)
            std_loss  = torch.nn.functional.mse_loss(input_std, self.target_std_filtered) # std_loss  = torch.nn.functional.mse_loss(input_std, self.target_std)
        # consider statistics from a subset of channels
        else:
            mean_loss = torch.nn.functional.mse_loss(input_mean[self.indices], self.target_mean_filtered[self.indices]) # mean_loss = torch.nn.functional.mse_loss(input_mean[self.indices], self.target_mean[self.indices])
            std_loss  = torch.nn.functional.mse_loss(input_std[self.indices], self.target_std_filtered[self.indices]) # std_loss  = torch.nn.functional.mse_loss(input_std[self.indices], self.target_std[self.indices])
        
        self.losses = {}
        self.losses['mean_loss'] = mean_loss
        self.losses['std_loss'] = std_loss
        
        return input

class StyleLossAmplifyFreq(torch.nn.Module):  
    """
    A simplified style loss layer which only computes style loss based on batch normalization statistics, 
    where std of speicific frequency is amplified
    """

    def __init__(self, target_style, sep_input_freq = True, amplify_freq = (None, None), amplify_weight = 10):
        super(StyleLossAmplifyFreq, self).__init__()

        # b: batch size, which should be 1
        # c: number of channels
        # h: height
        # w: width
        b, c, h, w = target_style.size()
        # print("c:", "{:3d}".format(c), "  h:", "{:3d}".format(h), "  w:", "{:3d}".format(w))
        style_feature = target_style.view(c, h * w)
        
        self.target_mean, self.target_std = BN_mean_and_std(style_feature)
        self.target_mean.detach_()
        self.target_std.detach_()

        # FFT filter kernel
        freq = torch.abs(torch.fft.rfftfreq(c))
        if amplify_freq[0] is None and amplify_freq[1] is None:
            self.kernel = freq <= 0 # fake value
        elif amplify_freq[0] is None:
            self.kernel = freq <= amplify_freq[1]
        elif amplify_freq[1] is None:
            self.kernel = freq >= amplify_freq[0]
        else:
            self.kernel = (freq <= amplify_freq[1]) * (freq >= amplify_freq[0])
        self.kernel = self.kernel.to(device).detach()
        
        # apply 1D FFT filter to std
        self.target_std_filtered = fft_filter_1D(self.target_std, kernel = self.kernel).detach()
        
        # other parameters
        self.sep_input_freq = sep_input_freq
        self.amplify_weight = amplify_weight if amplify_freq != (None,None) else 0

    ####################################### forward #######################################
    def forward(self, input):
        b, c, h, w = input.size()     # b = 1 since there is only one image in batch
        
        input_feature = input.view(c, h * w)
 
        input_mean, input_std = BN_mean_and_std(input_feature)
        
        # FFT filter to input std
        input_std_filtered = fft_filter_1D(input_std, kernel = self.kernel)
        
        # mean and std loss
        mean_loss = torch.nn.functional.mse_loss(input_mean, self.target_mean)
        std_loss = torch.nn.functional.mse_loss(input_std, self.target_std)

        # loss on filtered std
        if self.sep_input_freq:
            std_filtered_loss  = torch.nn.functional.mse_loss(input_std_filtered, self.target_std_filtered)
        else:
            std_filtered_loss = torch.nn.functional.mse_loss(input_std, self.target_std_filtered)
            

        self.losses = {}
        self.losses['mean_loss'] = mean_loss
        self.losses['std_loss'] = std_loss + std_filtered_loss * self.amplify_weight
        
        return input